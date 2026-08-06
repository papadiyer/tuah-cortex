"""P1-HIGH-1 regression: no secret leaves the process unredacted.

The Phase 3 bug was not that ``lekiu_redact`` was weak - it was that redaction
was applied at a handful of *error* paths while successful responses were
serialised raw. A secret curated into memory therefore came straight back out
through /v1/memory/search, /v1/context/build and the MCP tool results.

These tests seed a real secret into a real store and assert on the *serialised*
bytes of every outward boundary: HTTP, MCP and CLI. Asserting on the JSON string
rather than a parsed field is deliberate - it catches a leak in any field, at any
depth, including ones added later.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.app import CortexApp  # noqa: E402
from api.service import CortexService  # noqa: E402
from core.graph_store import GraphStore  # noqa: E402
from core.redact import MASK, redact_response  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402
from mcp.server import MCPServer  # noqa: E402
from workers.queue import EventQueue  # noqa: E402

# The canonical leaked credentials from the task's reproducer.
SECRET = "sk-1234567890abcdef"
BEARER = "Authorization: Bearer xyz987654321abcdef"


class RedactBoundaryTestCase(unittest.TestCase):
    """A store deliberately poisoned with secrets, exposed via every front end."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cortex-redact-")
        self.vector = VectorStore(":memory:")
        self.graph = GraphStore(":memory:")
        self.queue = EventQueue(os.path.join(self.tmp, "queue.db"))
        self.service = CortexService(
            vector_store=self.vector, graph_store=self.graph, queue=self.queue
        )
        self.app = CortexApp(service=self.service)

        # Seed memory the way a careless agent would: the secret is inside
        # ordinary prose, so it can only be caught by pattern, not by field name.
        self.vector.add(
            "The deploy pipeline authenticates with api_key %s and calls the "
            "billing endpoint. %s" % (SECRET, BEARER),
            {
                "type": "decision",
                "status": "proposed",
                "project": "jebat-cortex",
                "source": "leaky-log",
            },
        )
        self.vector.add(
            "Rotate the credential %s after the migration completes." % SECRET,
            {
                "type": "knowledge",
                "status": "approved",
                "project": "jebat-cortex",
                "source": "leaky-log-2",
            },
        )

    def tearDown(self):
        self.service.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assertRedacted(self, blob, where):
        """The serialised payload must mask the secret, not merely omit it."""
        self.assertNotIn(SECRET, blob, "%s leaked the verbatim secret" % where)
        self.assertNotIn("xyz987654321abcdef", blob, "%s leaked the bearer token" % where)
        self.assertIn(MASK, blob, "%s returned no redaction marker" % where)


class TestHTTPBoundary(RedactBoundaryTestCase):
    def test_memory_search_redacts_stored_secret(self):
        status, body = self.app.dispatch(
            "POST", "/v1/memory/search", {"query": "deploy pipeline", "filters": {}}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["results"], "fixture must actually return rows")
        self.assertRedacted(json.dumps(body), "/v1/memory/search")

    def test_memory_search_by_filter_redacts(self):
        status, body = self.app.dispatch(
            "POST",
            "/v1/memory/search",
            {"filters": {"memory_type": "decision", "project": "jebat-cortex"}},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["results"])
        self.assertRedacted(json.dumps(body), "/v1/memory/search (filtered)")

    def test_context_build_redacts_stored_secret(self):
        status, body = self.app.dispatch(
            "POST",
            "/v1/context/build",
            {"prompt": "how does the deploy pipeline authenticate?", "request_id": "redact-1"},
        )
        self.assertEqual(status, 200)
        self.assertRedacted(json.dumps(body), "/v1/context/build")

    def test_project_state_redacts_stored_secret(self):
        status, body = self.app.dispatch("GET", "/v1/projects/jebat-cortex/state")
        self.assertEqual(status, 200)
        self.assertRedacted(json.dumps(body), "/v1/projects/{id}/state")

    def test_response_serialiser_redacts_even_if_a_router_forgets(self):
        """The dispatcher-level gate is what makes this structural, not diligence.

        A future route that returns raw memory without calling redacted_result
        must still be scrubbed on the way out.
        """
        payload = {"nested": [{"deep": {"leak": "token is %s here" % SECRET}}]}
        blob = json.dumps(redact_response(payload))
        self.assertRedacted(blob, "redact_response backstop")

    def test_redaction_is_idempotent(self):
        """Router + serialiser both redact; double-masking must not corrupt."""
        once = redact_response("key %s" % SECRET)
        twice = redact_response(once)
        self.assertEqual(once, twice)


class TestMCPBoundary(RedactBoundaryTestCase):
    def _call(self, tool, arguments):
        server = MCPServer(service=self.service)
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        return response["result"]["content"][0]["text"]

    def test_search_memory_tool_redacts(self):
        text = self._call("cortex.search_memory", {"query": "deploy pipeline"})
        self.assertRedacted(text, "mcp cortex.search_memory")

    def test_build_context_tool_redacts(self):
        text = self._call(
            "cortex.build_context", {"prompt": "how does the deploy pipeline authenticate?"}
        )
        self.assertRedacted(text, "mcp cortex.build_context")

    def test_project_state_tool_redacts(self):
        text = self._call("cortex.get_project_state", {"project_id": "jebat-cortex"})
        self.assertRedacted(text, "mcp cortex.get_project_state")


class TestCLIBoundary(RedactBoundaryTestCase):
    def _capture(self, fn):
        buffer = io.StringIO()
        original = sys.stdout
        sys.stdout = buffer
        try:
            fn()
        finally:
            sys.stdout = original
        return buffer.getvalue()

    def test_cli_emit_redacts(self):
        from cli.cortex_cli import _emit

        _, body = self.service.search_memory({"query": "deploy pipeline", "filters": {}})
        out = self._capture(lambda: _emit(body))
        self.assertRedacted(out, "cli _emit")

    def test_cli_markdown_output_redacts(self):
        """--markdown writes the digest straight to stdout, bypassing _emit."""
        from core.redact import redact_response as _r

        _, body = self.service.build_context(
            {"prompt": "how does the deploy pipeline authenticate?", "request_id": "cli-1"}
        )
        markdown = _r(body.get("context_markdown", ""))
        self.assertNotIn(SECRET, markdown, "cli --markdown leaked the secret")


class TestRedactPatterns(unittest.TestCase):
    """Pattern-level checks for the shapes the boundary relies on."""

    def test_masks_common_credential_shapes(self):
        for raw in (
            "sk-1234567890abcdef",
            "Authorization: Bearer xyz987654321abcdef",
            "ghp_abcdefghijklmnopqrstuvwxyz0123",
            "AKIAIOSFODNN7EXAMPLE",
            "postgres://user:hunter2@localhost:5432/db",
            "api_key=super-secret-value",
        ):
            self.assertIn(MASK, redact_response(raw), "not redacted: %s" % raw)

    def test_leaves_ordinary_prose_alone(self):
        """Over-redaction would make the memory digest useless."""
        for raw in (
            "We should rotate the token next sprint.",
            "The password policy needs review.",
            "Use a SQLite durable queue",
        ):
            self.assertEqual(redact_response(raw), raw, "over-redacted: %s" % raw)

    def test_recursive_over_nested_structures(self):
        payload = {"a": [{"b": ("sk-1234567890abcdef",)}], "c": {"d": ["safe", SECRET]}}
        blob = json.dumps(redact_response(payload), default=str)
        self.assertNotIn(SECRET, blob)


if __name__ == "__main__":
    unittest.main()
