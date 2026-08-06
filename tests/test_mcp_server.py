"""Stdio MCP server: JSON-RPC protocol, the 7 tools, and the safety contract."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.service import CortexService  # noqa: E402
from core.graph_store import GraphStore  # noqa: E402
from core.rules import Embedder  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402
from mcp.server import TOOLS, MCPServer  # noqa: E402
from workers.queue import EventQueue  # noqa: E402

EXPECTED_TOOLS = {
    "cortex.search_memory",
    "cortex.build_context",
    "cortex.get_project_state",
    "cortex.get_decision_history",
    "cortex.get_related_experiences",
    "cortex.record_outcome",
    "cortex.health",
}


class _OtherBackendEmbedder(Embedder):
    name = "other-backend"

    @property
    def dimensions(self):
        return 512

    @property
    def model(self):
        return "other-model"

    def embed(self, text):
        return [1.0] + [0.0] * 511


class MCPTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cortex-mcp-")
        self.vector = VectorStore(":memory:")
        self.graph = GraphStore(":memory:")
        self.service = CortexService(
            vector_store=self.vector,
            graph_store=self.graph,
            queue=EventQueue(os.path.join(self.tmp, "queue.db")),
        )
        self.server = MCPServer(service=self.service)

    def tearDown(self):
        self.service.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, name, arguments=None, req_id=1):
        return self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )

    @staticmethod
    def _payload(response):
        """Decode the JSON carried in an MCP tool text content block."""
        return json.loads(response["result"]["content"][0]["text"])


class TestProtocol(MCPTestCase):
    def test_initialize_reports_protocol_and_server(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(response["result"]["serverInfo"]["name"], "jebat-cortex")
        self.assertIn("protocolVersion", response["result"])

    def test_ping(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(response["result"], {})

    def test_notification_returns_nothing(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_method_is_error(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_unknown_tool_is_reported_as_tool_error(self):
        response = self._call("cortex.destroy_everything")
        self.assertTrue(response["result"]["isError"])

    def test_stdio_loop_reads_and_writes_json_lines(self):
        stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
        stdout = io.StringIO()
        self.server.serve_stdio(stdin=stdin, stdout=stdout)

        lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(json.loads(lines[0])["result"]["tools"]), 7)

    def test_invalid_json_line_reports_parse_error(self):
        stdout = io.StringIO()
        self.server.serve_stdio(stdin=io.StringIO("{not json\n"), stdout=stdout)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], -32700)


class TestToolSurface(MCPTestCase):
    def test_exactly_the_seven_designed_tools(self):
        self.assertEqual({t["name"] for t in TOOLS}, EXPECTED_TOOLS)

    def test_no_destructive_admin_tools_exposed(self):
        names = {t["name"] for t in TOOLS}
        for forbidden in ("reindex", "admin", "delete", "drop", "shell", "exec"):
            self.assertFalse(
                any(forbidden in name.lower() for name in names),
                "MCP must not expose %s" % forbidden,
            )

    def test_every_tool_declares_an_input_schema(self):
        for tool in TOOLS:
            self.assertIn("inputSchema", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_tools_list_matches_registry(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual({t["name"] for t in response["result"]["tools"]}, EXPECTED_TOOLS)


class TestTools(MCPTestCase):
    def test_health_returns_healthy(self):
        payload = self._payload(self._call("cortex.health"))
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["identity"]["backend"], "deterministic")

    def test_build_context_returns_digest(self):
        payload = self._payload(self._call("cortex.build_context", {"prompt": "how does ranking work?"}))
        self.assertIn("context_markdown", payload)
        self.assertIn("provenance", payload)

    def test_build_context_requires_prompt(self):
        response = self._call("cortex.build_context", {})
        self.assertTrue(response["result"]["isError"])

    def test_search_memory_filters(self):
        self.vector.add(
            "Use a SQLite queue",
            {"source": "t", "type": "decision", "status": "approved", "project": "jebat-cortex"},
        )
        payload = self._payload(
            self._call("cortex.search_memory", {"memory_type": "decision", "status": "approved"})
        )
        self.assertEqual(payload["count"], 1)

    def test_get_decision_history_returns_only_approved(self):
        self.vector.add(
            "Approved decision", {"source": "t", "type": "decision", "status": "approved", "project": "p"}
        )
        self.vector.add(
            "Proposed decision", {"source": "t", "type": "decision", "status": "proposed", "project": "p"}
        )
        payload = self._payload(self._call("cortex.get_decision_history", {"project": "p"}))
        texts = [r["text"] for r in payload["results"]]
        self.assertIn("Approved decision", texts)
        self.assertNotIn("Proposed decision", texts)

    def test_get_related_experiences(self):
        self.graph.add_edge("core/vector_store.py", "imports", "sqlite3")
        payload = self._payload(
            self._call("cortex.get_related_experiences", {"entity": "vector_store"})
        )
        self.assertGreaterEqual(payload["count"], 1)

    def test_get_related_experiences_requires_entity(self):
        response = self._call("cortex.get_related_experiences", {"entity": ""})
        self.assertTrue(response["result"]["isError"])

    def test_get_project_state(self):
        self.vector.add(
            "A decision", {"source": "t", "type": "decision", "status": "approved", "project": "proj-x"}
        )
        payload = self._payload(self._call("cortex.get_project_state", {"project_id": "proj-x"}))
        self.assertEqual(payload["project_id"], "proj-x")
        self.assertEqual(len(payload["latest_approved_decisions"]), 1)

    def test_record_outcome_is_idempotent(self):
        event = {"request_id": "mcp-1", "agent": "lekiu", "result_summary": "done"}
        first = self._payload(self._call("cortex.record_outcome", event))
        second = self._payload(self._call("cortex.record_outcome", event))

        self.assertTrue(first["queued"])
        self.assertFalse(second["queued"])
        self.assertEqual(first["event_id"], second["event_id"])

    def test_record_outcome_requires_request_id(self):
        response = self._call("cortex.record_outcome", {"agent": "lekiu"})
        self.assertTrue(response["result"]["isError"])


class TestFailLoud(MCPTestCase):
    """MCP must surface an identity mismatch, not return empty memory."""

    def test_build_context_fails_loud_on_identity_mismatch(self):
        self.vector.add("stored with deterministic backend", {"source": "t"})
        self.vector.embedder = _OtherBackendEmbedder()

        response = self._call("cortex.build_context", {"prompt": "anything"})
        self.assertTrue(response["result"]["isError"], "mismatch must be an MCP tool error")
        payload = self._payload(response)
        self.assertEqual(payload["error"], "embedding_identity_mismatch")

    def test_degraded_store_is_reported_as_error(self):
        self.service.vector = None
        response = self._call("cortex.build_context", {"prompt": "anything"})
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(self._payload(response)["error"], "degraded")


if __name__ == "__main__":
    unittest.main()
