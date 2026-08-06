"""Context builder: merge behaviour and hard character-budget enforcement."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.context_builder import ContextBuilder  # noqa: E402
from core.graph_store import GraphStore  # noqa: E402
from core.rules import Embedder, load_rules, truncate  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402
from tests.support import closing  # noqa: E402

RULES = load_rules()
USER_LIMIT = RULES["limits"]["user_char_limit"]
MEMORY_LIMIT = RULES["limits"]["memory_char_limit"]


def _builder(testcase, use_ripgrep=False):
    """Build over two in-memory stores, all three closed by test cleanup.

    The stores are injected, so the builder does NOT own them (see
    ContextBuilder.own_stores) and will not close them - the test must, or the
    connections survive until GC and raise ResourceWarning.
    """
    return closing(
        testcase,
        ContextBuilder(
            vector_store=closing(testcase, VectorStore(":memory:")),
            graph_store=closing(testcase, GraphStore(":memory:")),
            use_ripgrep=use_ripgrep,
        ),
    )


class TestTruncate(unittest.TestCase):
    def test_never_exceeds_limit(self):
        for limit in (0, 1, 5, 20, 100, 999):
            self.assertLessEqual(len(truncate("word " * 500, limit)), limit)

    def test_short_text_untouched(self):
        self.assertEqual(truncate("hello", 100), "hello")


class TestBudgetEnforcement(unittest.TestCase):
    """Acceptance criteria: the two hard limits, proven on over-long input."""

    def setUp(self):
        self.builder = _builder(self)

    def test_limits_loaded_from_config(self):
        self.assertEqual(self.builder.user_char_limit, 1375)
        self.assertEqual(self.builder.memory_char_limit, 2200)

    def test_memory_block_never_exceeds_limit(self):
        # Retrieval caps candidates at top_k per store, so the entries themselves
        # must be large: 40 x ~1100 chars, of which the top 10 alone (~11k chars)
        # far exceed the 2200 budget.
        for i in range(40):
            self.builder.vector.add(
                "memory budget saturation entry %02d: %s" % (i, "sqlite vector ranking detail " * 38),
                {"source": "flood"},
            )
        for i in range(40):
            self.builder.graph.add_edge(
                "module_%02d.py" % i,
                "imports",
                "sqlite vector ranking dependency %02d %s" % (i, "extra path detail " * 30),
            )

        context = self.builder.build("sqlite vector ranking")
        candidates = context["counts"]["candidates"]
        raw_chars = sum(
            len(item["text"]) for item in context["knowledge"] + context["experience"]
        )

        # Guard the premise: the retrieved set must genuinely overflow the budget.
        self.assertGreater(candidates, 1, "need multiple candidates to test dropping")
        self.assertLessEqual(len(context["memory_block"]), MEMORY_LIMIT)
        self.assertEqual(context["memory_block_chars"], len(context["memory_block"]))
        self.assertTrue(context["memory_block"].strip(), "budget starved the block entirely")
        self.assertGreater(
            context["counts"]["dropped_for_budget"], 0, "oversized candidate set dropped nothing"
        )
        self.assertLessEqual(raw_chars, MEMORY_LIMIT)

    def test_prompt_slice_never_exceeds_limit(self):
        long_prompt = "explain the vector store ranking algorithm in detail. " * 100
        self.assertGreater(len(long_prompt), USER_LIMIT)

        context = self.builder.build(long_prompt)

        self.assertLessEqual(len(context["prompt"]), USER_LIMIT)
        self.assertEqual(context["prompt_chars"], len(context["prompt"]))
        self.assertTrue(context["prompt_truncated"])

    def test_markdown_memory_stays_within_limit(self):
        for i in range(30):
            self.builder.vector.add("knowledge overflow entry %d %s" % (i, "detail " * 60), {"source": "f"})
        context = self.builder.build("knowledge overflow entry")
        markdown = self.builder.to_markdown(context)

        self.assertLessEqual(context["memory_block_chars"], MEMORY_LIMIT)
        self.assertIn("## Jebat-Cortex Memory Digest", markdown)
        self.assertIn("### Memory", markdown)

    def test_single_oversized_entry_is_truncated_not_dropped(self):
        self.builder.vector.add("giant fact " * 900, {"source": "big"})
        context = self.builder.build("giant fact")

        self.assertLessEqual(len(context["memory_block"]), MEMORY_LIMIT)
        self.assertTrue(context["memory_block"].strip(), "oversized entry should be clipped, not lost")


class TestMergeAndOutput(unittest.TestCase):
    def setUp(self):
        self.builder = _builder(self)
        self.builder.vector.add("The memory char limit is 2200 characters", {"source": "cfg"})
        self.builder.vector.add("Faisal is the final approval authority", {"source": "cfg"})
        self.builder.graph.add_edge("core/context_builder.py", "imports", "core/vector_store.py")

    def test_merges_both_memory_types(self):
        context = self.builder.build("memory char limit for context_builder")
        self.assertGreater(context["counts"]["knowledge"], 0)
        self.assertGreater(context["counts"]["experience"], 0)
        types = {item["type"] for item in context["knowledge"] + context["experience"]}
        self.assertEqual(types, {"knowledge", "experience"})

    def test_results_are_score_ordered(self):
        context = self.builder.build("memory char limit")
        merged = context["knowledge"] + context["experience"]
        scores = sorted((item["score"] for item in merged), reverse=True)
        self.assertEqual(len(scores), len(merged))

    def test_json_serialisable(self):
        context = self.builder.build("memory char limit")
        self.assertIsInstance(json.dumps(context), str)

    def test_markdown_is_non_empty_and_reports_budget(self):
        markdown = self.builder.to_markdown(self.builder.build("memory char limit"))
        self.assertIn("**Budget:**", markdown)
        self.assertIn("2200", markdown)
        self.assertTrue(markdown.strip())

    def test_empty_stores_yield_graceful_digest(self):
        empty = _builder(self)
        context = empty.build("nothing has been learned yet")
        markdown = empty.to_markdown(context)
        self.assertEqual(context["counts"]["knowledge"], 0)
        self.assertIn("_no memory matched this prompt_", markdown)

    def test_empty_prompt_does_not_crash(self):
        context = self.builder.build("")
        self.assertEqual(context["prompt"], "")
        self.assertLessEqual(len(context["memory_block"]), MEMORY_LIMIT)


class TestRipgrepIntegration(unittest.TestCase):
    def test_fallback_used_only_when_graph_is_empty(self):
        builder = _builder(self, use_ripgrep=True)
        # Graph has nothing; the fallback should search the real repo.
        context = builder.build("VectorStore")
        self.assertLessEqual(len(context["memory_block"]), MEMORY_LIMIT)
        self.assertIsInstance(context["ripgrep_available"], bool)


class TestRetrievalFailsLoudOnIdentityMismatch(unittest.TestCase):
    """Regression for P1#1 final gap: production retrieval must NOT fail silent.

    query() defensively skips incompatible rows, but the production path
    (ContextBuilder.retrieve -> build) must raise when the knowledge store's
    embedding identity (backend/model/dim) differs from the active embedder -
    otherwise old memory silently disappears from the digest with no error.
    """

    def _seed_store(self, tmp_path, embedder):
        class _Store(Embedder):
            name = "fake"

            def __init__(self, dim, model):
                self._d = dim
                self.model_name = model

            @property
            def dimensions(self):
                return self._d

            @property
            def model(self):
                return self.model_name

            def embed(self, text):
                v = [0.0] * self._d
                for c in text:
                    v[ord(c) % self._d] += 1.0
                return v

        store = VectorStore(tmp_path, rules=RULES, embedder=_Store(3, "model-a"))
        store.add("legacy memory from model A", {"source": "s"})
        store.close()

    def _mismatched_builder(self):
        """Seed a store with model-a rows, then reopen it under model-b.

        Cleanups run last-registered-first, so the unlink registered here runs
        after the store closes - the file is never removed while a connection
        still holds it open.
        """
        import tempfile

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        self._seed_store(tmp.name, None)

        class _Other(Embedder):
            name = "fake"

            def __init__(self):
                self._d = 3
                self.model_name = "model-b"

            @property
            def dimensions(self):
                return self._d

            @property
            def model(self):
                return self.model_name

            def embed(self, text):
                v = [0.0] * self._d
                for c in text:
                    v[ord(c) % self._d] += 1.0
                return v

        return closing(
            self,
            ContextBuilder(
                vector_store=closing(self, VectorStore(tmp.name, rules=RULES, embedder=_Other())),
                graph_store=closing(self, GraphStore(":memory:")),
                use_ripgrep=False,
            ),
        )

    def test_build_raises_on_identity_mismatch(self):
        builder = self._mismatched_builder()
        with self.assertRaises(ValueError):
            builder.build("legacy memory from model A")

    def test_retrieve_raises_before_query(self):
        builder = self._mismatched_builder()
        with self.assertRaises(ValueError):
            builder.retrieve("legacy memory from model A")


if __name__ == "__main__":
    unittest.main()
