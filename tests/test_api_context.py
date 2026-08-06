"""POST /v1/context/build: tiers, budget enforcement, provenance, fail-loud 409.

Also covers POST /v1/memory/search filtering and GET /v1/projects/{id}/state,
since they share the retrieval path.
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
from core.rules import Embedder, load_rules  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402
from workers.queue import EventQueue  # noqa: E402

RULES = load_rules()
MEMORY_LIMIT = RULES["limits"]["memory_char_limit"]


class _OtherBackendEmbedder(Embedder):
    """A different embedding identity, used to prove the 409 fail-loud path."""

    name = "other-backend"

    @property
    def dimensions(self):
        return 512

    @property
    def model(self):
        return "other-model"

    def embed(self, text):
        return [1.0] + [0.0] * 511


class ContextTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cortex-ctx-")
        self.vector = VectorStore(":memory:")
        self.graph = GraphStore(":memory:")
        self.service = CortexService(
            vector_store=self.vector,
            graph_store=self.graph,
            queue=EventQueue(os.path.join(self.tmp, "queue.db")),
        )
        self.app = CortexApp(service=self.service)

    def tearDown(self):
        self.service.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build(self, prompt="how does the vector store rank results?", **extra):
        body = {"prompt": prompt, "request_id": "req-ctx"}
        body.update(extra)
        return self.app.dispatch("POST", "/v1/context/build", body)


class TestContextBuild(ContextTestCase):
    def test_returns_full_preflight_payload(self):
        status, body = self._build()
        self.assertEqual(status, 200)
        for field in (
            "request_id",
            "resolved_project",
            "identity",
            "relevant_experiences",
            "relevant_knowledge",
            "relevant_decisions",
            "open_tasks",
            "recommended_agent",
            "context_markdown",
            "provenance",
            "warnings",
        ):
            self.assertIn(field, body, "preflight must return %s" % field)

    def test_tier0_is_present_without_any_stored_memory(self):
        """Tier 0 comes from config, so it survives an empty store."""
        status, body = self._build()
        self.assertEqual(status, 200)
        self.assertEqual(body["tier0"]["final_authority"], "Faisal")
        self.assertIn("Faisal", body["context_markdown"])

    def test_tier1_memory_is_retrieved(self):
        self.vector.add("The vector store ranks results by cosine similarity", {"source": "t"})
        _, body = self._build()
        self.assertTrue(body["relevant_knowledge"], "stored knowledge should be retrieved")

    def test_provenance_is_returned_for_retrieved_memory(self):
        self.vector.add("The vector store ranks results by cosine similarity", {"source": "t"})
        _, body = self._build()
        self.assertTrue(body["provenance"])
        entry = body["provenance"][0]
        self.assertIn("source_type", entry)
        self.assertIn("status", entry)

    def test_tier2_is_not_auto_injected(self):
        _, body = self._build()
        self.assertIn("not injected", body["context_markdown"])

    def test_recommended_agent_defaults_to_jebat(self):
        _, body = self._build(prompt="   ")
        self.assertEqual(body["recommended_agent"], "jebat")

    def test_resolved_project_uses_hint(self):
        _, body = self._build(project_hint="jebat-cortex")
        self.assertEqual(body["resolved_project"], "jebat-cortex")


class TestBudget(ContextTestCase):
    """The digest must never exceed the supplied budget (task constraint 2)."""

    def _flood(self):
        for i in range(40):
            self.vector.add(
                "budget saturation entry %02d: %s" % (i, "vector ranking detail " * 40),
                {"source": "flood"},
            )

    def test_respects_small_token_budget(self):
        self._flood()
        _, body = self._build(token_budget=100)
        # 100 tokens ~ 400 chars of memory; the frame is bounded on top.
        self.assertLessEqual(body["memory_char_budget"], 400)
        self.assertLessEqual(len(body["context_markdown"]), 400 + 1375 + 1200)

    def test_hard_cap_wins_over_large_budget(self):
        self._flood()
        _, body = self._build(token_budget=8000)
        self.assertLessEqual(
            body["memory_char_budget"], MEMORY_LIMIT, "configured hard cap must win"
        )

    def test_budget_is_enforced_on_flooded_store(self):
        self._flood()
        _, body = self._build(token_budget=200)
        self.assertLessEqual(body["memory_char_budget"], 800)
        self.assertGreater(body["counts"]["dropped_for_budget"], 0)

    def test_token_budget_over_maximum_is_rejected(self):
        status, body = self._build(token_budget=99999)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")

    def test_digest_never_exceeds_the_requested_budget(self):
        """API_CONTRACTS: the response must never exceed the requested budget.

        Regression: the digest cap used to add user_char_limit + 1200 on top of
        the requested budget, so a small token_budget was blown by >1000 chars.
        """
        self._flood()
        for token_budget in (10, 25, 50, 100, 200, 400):
            _, body = self._build(token_budget=token_budget)
            self.assertLessEqual(
                len(body["context_markdown"]),
                token_budget * 4,
                "token_budget=%d produced a %d-char digest (cap %d)"
                % (token_budget, len(body["context_markdown"]), token_budget * 4),
            )

    def test_truncation_to_budget_is_flagged_in_warnings(self):
        """A clipped digest must say so, not look complete."""
        self._flood()
        _, body = self._build(token_budget=10)
        self.assertIn("context_truncated_to_token_budget", body["warnings"])

    def test_tier0_survives_a_tiny_budget(self):
        """Tier 0 renders first, so authority context is the last thing dropped."""
        self._flood()
        _, body = self._build(token_budget=60)
        self.assertIn("Faisal", body["context_markdown"])


class TestProjectScoping(ContextTestCase):
    """Regression: Tier 1 leaked other projects' memory into the digest."""

    def setUp(self):
        super().setUp()
        self.vector.add(
            "CLIENT-X migration plan: drop the legacy payments table",
            {"source": "e", "type": "knowledge", "status": "approved", "project": "client-x"},
        )
        self.vector.add(
            "jebat-cortex uses a SQLite durable queue for events",
            {"source": "e", "type": "knowledge", "status": "approved", "project": "jebat-cortex"},
        )
        self.vector.add(
            "Faisal prefers short direct answers with test evidence",
            {"source": "e", "type": "knowledge", "status": "approved"},  # global, no project
        )
        self.graph.add_edge(
            "lekiu", "produced", "client-x/payments_migration.sql",
            source="e", meta={"project": "client-x"},
        )

    def test_other_projects_memory_is_not_retrieved(self):
        _, body = self._build(
            prompt="migration plan payments table queue", project_hint="jebat-cortex"
        )
        texts = " ".join(k["text"] for k in body["relevant_knowledge"])
        self.assertNotIn("CLIENT-X", texts, "another project's memory must not be injected")

    def test_other_projects_experience_is_not_retrieved(self):
        _, body = self._build(
            prompt="payments migration sql", project_hint="jebat-cortex"
        )
        texts = " ".join(e["text"] for e in body["relevant_experiences"])
        self.assertNotIn("client-x", texts)

    def test_global_memory_is_still_retrieved(self):
        """Project-less memory applies everywhere and must survive scoping."""
        _, body = self._build(
            prompt="Faisal prefers short direct answers", project_hint="jebat-cortex"
        )
        texts = " ".join(k["text"] for k in body["relevant_knowledge"])
        self.assertIn("Faisal prefers", texts)

    def test_unscoped_request_still_sees_everything(self):
        _, body = self._build(prompt="migration plan payments table")
        self.assertIsNone(body["resolved_project"])
        texts = " ".join(k["text"] for k in body["relevant_knowledge"])
        self.assertIn("CLIENT-X", texts, "with no project resolved, nothing is filtered out")


class TestRetrievalFailureIsLoud(ContextTestCase):
    """Regression: a store error looked identical to 'no decisions exist'."""

    def test_structured_lookup_failure_raises_a_warning(self):
        def boom(*args, **kwargs):
            raise RuntimeError("disk I/O error")

        self.vector.search = boom
        status, body = self._build()

        self.assertEqual(status, 200, "the digest is still served")
        self.assertEqual(body["relevant_decisions"], [])
        self.assertTrue(
            any("decision_lookup_failed" in w for w in body["warnings"]),
            "an empty decision list caused by an error must be flagged: %s" % body["warnings"],
        )

    def test_identity_error_still_reaches_the_409_path(self):
        """The broad except must not swallow the embedding mismatch."""
        self.vector.add("stored under deterministic", {"source": "t"})
        self.vector.embedder = _OtherBackendEmbedder()
        status, body = self._build()
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "embedding_identity_mismatch")


class TestEntityFilterEscaping(ContextTestCase):
    """Regression: entity='%' matched the entire store."""

    def test_like_metacharacters_are_literal(self):
        for text in ("alpha one", "beta two", "gamma three"):
            self.vector.add(text, {"source": "e", "type": "knowledge"})

        _, wildcard = self.app.dispatch("POST", "/v1/memory/search", {"filters": {"entity": "%"}})
        self.assertEqual(wildcard["count"], 0, "'%' must be a literal, not match-everything")

        _, underscore = self.app.dispatch("POST", "/v1/memory/search", {"filters": {"entity": "_"}})
        self.assertEqual(underscore["count"], 0)

        _, real = self.app.dispatch("POST", "/v1/memory/search", {"filters": {"entity": "alpha"}})
        self.assertEqual(real["count"], 1, "ordinary entity search still works")


class TestValidation(ContextTestCase):
    def test_missing_prompt_is_400(self):
        status, body = self.app.dispatch("POST", "/v1/context/build", {"request_id": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")

    def test_oversized_prompt_is_400(self):
        status, body = self._build(prompt="x" * 40000)
        self.assertEqual(status, 400)

    def test_non_object_body_is_400(self):
        status, _ = self.app.dispatch("POST", "/v1/context/build", ["not", "an", "object"])
        self.assertEqual(status, 400)

    def test_bad_token_budget_type_is_400(self):
        status, _ = self._build(token_budget="lots")
        self.assertEqual(status, 400)


class TestFailLoud(ContextTestCase):
    """An identity mismatch must be a hard 409, never a silent empty digest."""

    def test_identity_mismatch_returns_409(self):
        self.vector.add("stored under the deterministic backend", {"source": "t"})
        # Swap the embedder underneath the store: stored rows no longer match.
        self.vector.embedder = _OtherBackendEmbedder()

        status, body = self._build()
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "embedding_identity_mismatch")

    def test_degraded_stores_return_503(self):
        self.service.vector = None
        status, body = self._build()
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "degraded")
        self.assertEqual(body["memory_status"], "degraded")

    def test_search_also_fails_loud_on_mismatch(self):
        self.vector.add("stored under the deterministic backend", {"source": "t"})
        self.vector.embedder = _OtherBackendEmbedder()
        status, body = self.app.dispatch("POST", "/v1/memory/search", {"query": "anything"})
        self.assertEqual(status, 409)


class TestMemorySearch(ContextTestCase):
    def setUp(self):
        super().setUp()
        self.vector.add(
            "Use a SQLite queue",
            {"source": "e", "type": "decision", "status": "approved", "project": "jebat-cortex"},
        )
        self.vector.add(
            "Maybe use Redis",
            {"source": "e", "type": "decision", "status": "proposed", "project": "jebat-cortex"},
        )
        self.vector.add(
            "Other project decision",
            {"source": "e", "type": "decision", "status": "approved", "project": "other"},
        )

    def test_filters_by_status(self):
        _, body = self.app.dispatch(
            "POST", "/v1/memory/search", {"filters": {"memory_type": "decision", "status": "approved"}}
        )
        texts = [r["text"] for r in body["results"]]
        self.assertIn("Use a SQLite queue", texts)
        self.assertNotIn("Maybe use Redis", texts, "a proposal must not appear as approved")

    def test_filters_by_project(self):
        _, body = self.app.dispatch(
            "POST", "/v1/memory/search", {"filters": {"project": "jebat-cortex", "memory_type": "decision"}}
        )
        self.assertEqual(len(body["results"]), 2)

    def test_results_carry_provenance(self):
        _, body = self.app.dispatch("POST", "/v1/memory/search", {"filters": {"memory_type": "decision"}})
        self.assertTrue(body["results"])
        self.assertIn("provenance", body["results"][0])
        self.assertIn("source_type", body["results"][0]["provenance"])

    def test_superseded_is_excluded_by_default(self):
        row_id = self.vector.add(
            "Old approach", {"source": "e", "type": "decision", "project": "jebat-cortex"}
        )
        self.vector.set_status(row_id, "superseded")
        _, body = self.app.dispatch(
            "POST", "/v1/memory/search", {"filters": {"memory_type": "decision"}}
        )
        self.assertNotIn("Old approach", [r["text"] for r in body["results"]])

    def test_invalid_status_filter_is_400(self):
        status, _ = self.app.dispatch(
            "POST", "/v1/memory/search", {"filters": {"status": "not-a-status"}}
        )
        self.assertEqual(status, 400)


class TestProjectState(ContextTestCase):
    def test_separates_approved_from_unresolved(self):
        self.vector.add(
            "Approved thing",
            {"source": "e", "type": "decision", "status": "approved", "project": "p1"},
        )
        self.vector.add(
            "Proposed thing",
            {"source": "e", "type": "decision", "status": "proposed", "project": "p1"},
        )
        status, body = self.app.dispatch("GET", "/v1/projects/p1/state")

        self.assertEqual(status, 200)
        self.assertEqual(body["project_id"], "p1")
        self.assertIn("Approved thing", [d["text"] for d in body["latest_approved_decisions"]])
        self.assertIn("Proposed thing", [d["text"] for d in body["unresolved_decisions"]])
        self.assertNotIn(
            "Proposed thing",
            [d["text"] for d in body["latest_approved_decisions"]],
            "a proposal must never be listed as an approved decision",
        )


if __name__ == "__main__":
    unittest.main()
