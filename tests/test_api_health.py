"""GET /v1/health, routing, localhost-bind enforcement and admin auth."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.app import CortexApp, create_server  # noqa: E402
from api.service import CortexService  # noqa: E402
from core.graph_store import GraphStore  # noqa: E402
from core.rules import SERVICE_VERSION  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402
from workers.queue import EventQueue  # noqa: E402


class AppTestCase(unittest.TestCase):
    """In-memory stores + a temp queue, so tests never touch real memory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cortex-api-")
        self.service = CortexService(
            vector_store=VectorStore(":memory:"),
            graph_store=GraphStore(":memory:"),
            queue=EventQueue(os.path.join(self.tmp, "queue.db")),
        )
        self.app = CortexApp(service=self.service)

    def tearDown(self):
        self.service.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestHealth(AppTestCase):
    def test_health_is_healthy_with_working_stores(self):
        status, body = self.app.dispatch("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "healthy")

    def test_health_reports_required_fields(self):
        _, body = self.app.dispatch("GET", "/v1/health")
        for field in (
            "status",
            "version",
            "graph_store",
            "vector_store",
            "embedding_identity",
            "queue_depth",
            "last_ingestion",
            "uptime_seconds",
        ):
            self.assertIn(field, body, "health must report %s" % field)

    def test_health_reports_embedding_identity(self):
        _, body = self.app.dispatch("GET", "/v1/health")
        # v1.1 ships the semantic backend as default.
        self.assertEqual(body["identity"]["backend"], "sentence-transformers")
        self.assertEqual(body["identity"]["dim"], 384)

    def test_version_matches_service_version(self):
        _, body = self.app.dispatch("GET", "/v1/health")
        self.assertEqual(body["version"], SERVICE_VERSION)

    def test_queue_depth_tracks_enqueued_events(self):
        self.app.dispatch(
            "POST", "/v1/events/postflight", {"request_id": "h1", "result_summary": "x"}
        )
        _, body = self.app.dispatch("GET", "/v1/health")
        self.assertEqual(body["queue_depth"], 1)

    def test_degraded_when_a_store_is_down(self):
        self.service.graph = None
        _, body = self.app.dispatch("GET", "/v1/health")
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["memory_status"], "degraded")


class TestRouting(AppTestCase):
    def test_unknown_route_is_404(self):
        status, body = self.app.dispatch("GET", "/v1/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_wrong_method_is_405(self):
        status, body = self.app.dispatch("POST", "/v1/health")
        self.assertEqual(status, 405)
        self.assertEqual(body["error"], "method_not_allowed")

    def test_trailing_slash_is_tolerated(self):
        status, _ = self.app.dispatch("GET", "/v1/health/")
        self.assertEqual(status, 200)

    def test_query_string_is_ignored_for_matching(self):
        status, _ = self.app.dispatch("GET", "/v1/health?verbose=1")
        self.assertEqual(status, 200)


class TestOversizedBody(unittest.TestCase):
    """Regression: a 413 left the unread body in the socket, desyncing HTTP/1.1."""

    def setUp(self):
        import threading

        self.server, self.app = create_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()

    def test_oversized_body_returns_413_and_closes_cleanly(self):
        import socket

        body = b'{"a":"' + b"x" * 300000 + b'"}'
        request = (
            b"POST /v1/events/postflight HTTP/1.1\r\n"
            b"Host: localhost\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\n\r\n" % len(body)
        ) + body

        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            sock.sendall(request)
            received = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                received += chunk
        finally:
            sock.close()

        self.assertIn(b"413", received.split(b"\r\n")[0])
        self.assertIn(b"Connection: close", received)
        # Exactly one response: the leftover body must not be parsed as a
        # second request line (which used to produce a spurious 414).
        self.assertEqual(received.count(b"HTTP/1.1 "), 1, received[:200])
        self.assertNotIn(b"414", received)


class TestLocalhostBinding(unittest.TestCase):
    """Task constraint 4: the API must never bind a public interface."""

    def test_refuses_wildcard_bind(self):
        with self.assertRaises(ValueError):
            create_server("0.0.0.0", 8799)

    def test_refuses_public_ip(self):
        with self.assertRaises(ValueError):
            create_server("192.168.1.50", 8799)

    def test_refuses_hostname(self):
        # Hostnames could resolve off-loopback, so only IP literals are allowed.
        with self.assertRaises(ValueError):
            create_server("localhost", 8799)

    def test_accepts_loopback(self):
        server, app = create_server("127.0.0.1", 0)
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        finally:
            server.server_close()
            app.close()


class TestDegradedBoot(unittest.TestCase):
    """Degraded startup must not leak handles or touch production stores."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cortex-degraded-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_partial_store_failure_closes_the_store_that_opened(self):
        """Regression: vector opened, graph failed, vector was orphaned open."""
        import sqlite3

        import core.graph_store as gsmod
        import core.vector_store as vsmod

        captured = {}
        original_vector_init = vsmod.VectorStore.__init__
        original_graph_init = gsmod.GraphStore.__init__

        def capture_vector(self, *args, **kwargs):
            original_vector_init(self, *args, **kwargs)
            captured["vector"] = self

        def fail_graph(self, *args, **kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        vsmod.VectorStore.__init__ = capture_vector
        gsmod.GraphStore.__init__ = fail_graph
        try:
            service = CortexService(queue=EventQueue(os.path.join(self.tmp, "q.db")))
        finally:
            vsmod.VectorStore.__init__ = original_vector_init
            gsmod.GraphStore.__init__ = original_graph_init
        self.addCleanup(service.close)

        self.assertIsNone(service.vector)
        self.assertIsNone(service.graph)
        opened = captured.get("vector")
        self.assertIsNotNone(opened, "the vector store was constructed")
        with self.assertRaises(sqlite3.ProgrammingError):
            opened.conn.execute("SELECT 1")

    def test_health_still_answers_when_degraded(self):
        service = CortexService(
            vector_store=VectorStore(":memory:"),
            graph_store=GraphStore(":memory:"),
            queue=EventQueue(os.path.join(self.tmp, "q2.db")),
        )
        self.addCleanup(service.close)
        service.vector = None
        status, body = service.health()
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "degraded")

    def test_drain_queue_refuses_when_degraded(self):
        """Regression: it silently opened the real production databases."""
        import sqlite3

        service = CortexService(
            vector_store=VectorStore(":memory:"),
            graph_store=GraphStore(":memory:"),
            queue=EventQueue(os.path.join(self.tmp, "q3.db")),
        )
        self.addCleanup(service.close)
        service.vector = None
        service.graph = None

        opened = []
        real_connect = sqlite3.connect

        def spy(path, *args, **kwargs):
            opened.append(str(path))
            return real_connect(path, *args, **kwargs)

        sqlite3.connect = spy
        try:
            result = service.drain_queue()
        finally:
            sqlite3.connect = real_connect

        self.assertEqual(result["error"], "degraded")
        self.assertEqual(result["processed"], 0)
        self.assertEqual(opened, [], "must not open any database while degraded")


class TestAdminAuth(AppTestCase):
    def test_admin_disabled_without_token_configured(self):
        self.service.admin_token = None
        status, body = self.app.dispatch("GET", "/v1/admin/queue")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "admin_disabled")

    def test_admin_rejects_wrong_token(self):
        self.service.admin_token = "correct-horse"
        status, body = self.app.dispatch(
            "GET", "/v1/admin/queue", headers={"Authorization": "Bearer wrong"}
        )
        self.assertEqual(status, 401)

    def test_admin_accepts_correct_token(self):
        self.service.admin_token = "correct-horse"
        status, body = self.app.dispatch(
            "GET", "/v1/admin/queue", headers={"Authorization": "Bearer correct-horse"}
        )
        self.assertEqual(status, 200)
        self.assertIn("stats", body)

    def test_reindex_is_non_destructive(self):
        self.service.vector.add("keep me", {"source": "t"})
        self.service.admin_token = "tok"
        status, body = self.app.dispatch(
            "POST", "/v1/admin/reindex", {}, headers={"X-Admin-Token": "tok"}
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["reindexed"], "reindex must not rewrite memory")
        self.assertEqual(self.service.vector.count(), 1, "memory must survive reindex")


if __name__ == "__main__":
    unittest.main()
