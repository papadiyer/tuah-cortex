"""P2-MEDIUM regression: GET /v1/events/{event_id} lifecycle read-back.

Phase 3 accepted events with 202 and no way to ask what became of them: a caller
could not distinguish "still queued" from "dead-lettered". This route closes that
gap by reusing EventQueue.by_event_id().

Deliberate limit under test: the response carries status + timestamps +
provenance ONLY. Dumping the stored payload would hand back the original prompt
and every curated decision, reopening exactly the leak P1-HIGH-1 closes.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.app import CortexApp  # noqa: E402
from api.service import CortexService  # noqa: E402
from core.graph_store import GraphStore  # noqa: E402
from core.memory_curator import Curator  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402
from workers.ingest_worker import IngestWorker  # noqa: E402
from workers.queue import EventQueue  # noqa: E402

SECRET = "sk-1234567890abcdef"


class EventReadbackTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cortex-readback-")
        self.vector = VectorStore(":memory:")
        self.graph = GraphStore(":memory:")
        self.queue = EventQueue(os.path.join(self.tmp, "queue.db"))
        self.service = CortexService(
            vector_store=self.vector, graph_store=self.graph, queue=self.queue
        )
        self.app = CortexApp(service=self.service)

    def tearDown(self):
        self.service.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, request_id="readback-1", **extra):
        body = {
            "request_id": request_id,
            "actor": "faisal",
            "agent": "lekiu",
            "project": "jebat-cortex",
            "prompt": "wire the read-back route",
            "result_summary": "done",
            "decisions": [{"text": "Reuse by_event_id", "status": "proposed"}],
            "lessons": [{"text": "Observability is not optional"}],
        }
        body.update(extra)
        status, response = self.app.dispatch("POST", "/v1/events/postflight", body)
        self.assertEqual(status, 202)
        return response["event_id"]

    def _get(self, event_id):
        return self.app.dispatch("GET", "/v1/events/%s" % event_id)

    def _drain(self):
        curator = Curator(
            rules=self.service.rules, vector_store=self.vector, graph_store=self.graph
        )
        return IngestWorker(
            queue=self.queue, curator=curator, rules=self.service.rules
        ).drain()


class TestLifecycleStates(EventReadbackTestCase):
    def test_freshly_posted_event_reads_back_as_queued(self):
        event_id = self._post()
        status, body = self._get(event_id)
        self.assertEqual(status, 200)
        self.assertEqual(body["event_id"], event_id)
        self.assertEqual(body["state"], "queued")
        self.assertEqual(body["request_id"], "readback-1")
        self.assertTrue(body["created_at"])
        self.assertTrue(body["updated_at"])

    def test_state_becomes_done_after_the_worker_runs(self):
        event_id = self._post()
        self._drain()
        status, body = self._get(event_id)
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "done")

    def test_dead_lettered_event_reads_back_as_dead(self):
        class _Boom:
            def ingest_event(self, event):
                raise RuntimeError("ingest exploded")

            def close(self):
                pass

        queue = EventQueue(os.path.join(self.tmp, "dead.db"), max_attempts=1)
        self.addCleanup(queue.close)
        service = CortexService(
            vector_store=self.vector, graph_store=self.graph, queue=queue
        )
        app = CortexApp(service=service)
        _, posted = app.dispatch(
            "POST", "/v1/events/postflight", {"request_id": "doomed-1"}
        )
        worker = IngestWorker(queue=queue, curator=_Boom(), rules=service.rules)
        worker.process_one()

        status, body = app.dispatch("GET", "/v1/events/%s" % posted["event_id"])
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "dead")
        self.assertIn("last_error", body)

    def test_unknown_event_id_is_404(self):
        status, body = self._get("evt_does-not-exist")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_attempts_are_reported(self):
        event_id = self._post()
        _, body = self._get(event_id)
        self.assertEqual(body["attempts"], 0)
        self._drain()
        _, after = self._get(event_id)
        self.assertEqual(after["attempts"], 1)


class TestReadbackDisclosure(EventReadbackTestCase):
    """The route must be observability, not a payload dump."""

    def test_provenance_is_returned(self):
        event_id = self._post()
        _, body = self._get(event_id)
        provenance = body["provenance"]
        self.assertEqual(provenance["actor"], "faisal")
        self.assertEqual(provenance["agent"], "lekiu")
        self.assertEqual(provenance["project"], "jebat-cortex")

    def test_counts_are_returned_without_the_content(self):
        event_id = self._post()
        _, body = self._get(event_id)
        self.assertEqual(body["counts"]["decisions"], 1)
        self.assertEqual(body["counts"]["lessons"], 1)

    def test_original_prompt_is_not_echoed(self):
        event_id = self._post()
        _, body = self._get(event_id)
        blob = str(body)
        self.assertNotIn("wire the read-back route", blob, "prompt must not be echoed")
        self.assertNotIn("Reuse by_event_id", blob, "decision text must not be echoed")

    def test_secret_in_the_payload_is_never_echoed(self):
        event_id = self._post(
            "secret-evt",
            prompt="deploy with api_key %s" % SECRET,
            result_summary="used %s" % SECRET,
        )
        _, body = self._get(event_id)
        self.assertNotIn(SECRET, str(body))

    def test_degraded_queue_returns_503(self):
        event_id = self._post()
        self.service.queue = None
        status, body = self._get(event_id)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "degraded")


class TestRoutingDoesNotShadowPostflight(EventReadbackTestCase):
    """{event_id} must not swallow the postflight path."""

    def test_postflight_still_routes_on_post(self):
        status, _ = self.app.dispatch(
            "POST", "/v1/events/postflight", {"request_id": "still-works"}
        )
        self.assertEqual(status, 202)

    def test_get_on_postflight_path_is_a_lookup_miss_not_a_crash(self):
        status, _ = self.app.dispatch("GET", "/v1/events/postflight")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
