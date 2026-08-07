"""Pluggable embedder: deterministic default, lazy optional backend."""

import copy
import json
import os
import sys
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.rules as rules_module  # noqa: E402
from core.rules import (  # noqa: E402
    DEFAULT_BACKEND,
    DeterministicEmbedder,
    Embedder,
    EmbedderUnavailableError,
    SentenceTransformerEmbedder,
    SENTENCE_TRANSFORMERS_BACKEND,
    cosine,
    embed,
    get_embedder,
    load_rules,
    sentence_transformers_available,
)
from core.vector_store import VectorStore  # noqa: E402


class TestDeterministicEmbedder(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()

    def test_matches_legacy_embed_function(self):
        """Regression: refactoring must not change a single vector component."""
        texts = [
            "Faisal prefers concise answers",
            "core/vector_store.py imports core/rules.py",
            "the memory char limit is 2200",
            "",
            "ünïcode and pathy/things.py mixed 12345",
        ]
        embedder = DeterministicEmbedder(self.rules)
        for text in texts:
            self.assertEqual(embedder.embed(text), embed(text, self.rules), text)

    def test_dimensions_matches_config(self):
        embedder = DeterministicEmbedder(self.rules)
        # Deterministic is now the opt-in fallback; its dims come from
        # fallback_dimensions (the lexical `dimensions` key was removed on flip).
        dim_key = "dimensions" if "dimensions" in self.rules["embedding"] else "fallback_dimensions"
        self.assertEqual(embedder.dimensions, int(self.rules["embedding"][dim_key]))
        self.assertEqual(len(embedder.embed("anything")), embedder.dimensions)

    def test_is_an_embedder_and_callable(self):
        embedder = DeterministicEmbedder(self.rules)
        self.assertIsInstance(embedder, Embedder)
        self.assertEqual(embedder("same text"), embedder.embed("same text"))

    def test_output_is_normalised_and_deterministic(self):
        embedder = DeterministicEmbedder(self.rules)
        first = embedder.embed("sqlite database storage")
        second = embedder.embed("sqlite database storage")
        self.assertEqual(first, second)
        self.assertAlmostEqual(sum(v * v for v in first) ** 0.5, 1.0, places=6)

    def test_loads_rules_when_none_given(self):
        self.assertEqual(DeterministicEmbedder().dimensions, 512)


class TestGetEmbedder(unittest.TestCase):
    def setUp(self):
        self.rules = copy.deepcopy(load_rules())

    def test_defaults_to_semantic_when_configured(self):
        # v1.1 ships the semantic backend as the default; get_embedder returns it.
        self.assertIsInstance(get_embedder(self.rules), SentenceTransformerEmbedder)

    def test_config_ships_semantic_backend(self):
        self.assertEqual(
            load_rules()["embedding"].get("backend"), SENTENCE_TRANSFORMERS_BACKEND
        )

    def test_missing_backend_key_still_deterministic(self):
        self.rules["embedding"].pop("backend", None)
        self.assertIsInstance(get_embedder(self.rules), DeterministicEmbedder)

    def test_unknown_backend_raises(self):
        self.rules["embedding"]["backend"] = "magic-beans"
        with self.assertRaises(ValueError):
            get_embedder(self.rules)

    def test_semantic_backend_fails_loud_on_unloadable_model(self):
        """A configured semantic backend whose model cannot load must RAISE,
        not silently degrade to lexical (P1 fail-loud)."""
        bad = copy.deepcopy(self.rules)
        bad["embedding"]["model"] = "this-model-does-not-exist-xyz"
        with self.assertRaises(EmbedderUnavailableError):
            get_embedder(bad, allow_fallback=False)

    def test_semantic_backend_degrades_only_when_explicitly_allowed(self):
        """Opt-in degrade path still works for dev boxes without the model."""
        bad = copy.deepcopy(self.rules)
        bad["embedding"]["model"] = "this-model-does-not-exist-xyz"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            embedder = get_embedder(bad, allow_fallback=True)
        self.assertIsInstance(embedder, DeterministicEmbedder)
        self.assertTrue(
            any("sentence-transformers" in str(w.message) for w in caught),
            "expected a warning naming the unavailable backend",
        )


class TestSentenceTransformerLaziness(unittest.TestCase):
    def test_module_imports_without_the_package(self):
        """core.rules must import cleanly on a host with no sentence-transformers.

        On a host where ST IS installed (our venv), skip - the assertion about
        absence cannot hold.
        """
        if sentence_transformers_available():
            self.skipTest("sentence-transformers is installed on this host")
        self.assertNotIn("sentence_transformers", sys.modules)
        self.assertTrue(hasattr(rules_module, "SentenceTransformerEmbedder"))

    def test_instantiation_raises_clear_error_when_absent(self):
        if sentence_transformers_available():
            self.skipTest("sentence-transformers is installed on this host")
        with self.assertRaises(EmbedderUnavailableError) as ctx:
            SentenceTransformerEmbedder("all-MiniLM-L6-v2")
        self.assertIn("unavailable", str(ctx.exception).lower())

    def test_availability_probe_never_raises(self):
        self.assertIsInstance(sentence_transformers_available(), bool)


class _StubEmbedder(Embedder):
    """Fixed 4-dim embedder: proves the store uses the injected backend."""

    name = "stub"

    def __init__(self):
        self.calls = []

    @property
    def dimensions(self):
        return 4

    def embed(self, text):
        self.calls.append(text)
        if "limit" in (text or "").lower():
            return [1.0, 0.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0, 0.0]


class TestVectorStoreEmbedderInjection(unittest.TestCase):
    def test_defaults_to_semantic_embedder(self):
        store = VectorStore(":memory:")
        try:
            self.assertIsInstance(store.embedder, SentenceTransformerEmbedder)
        finally:
            store.close()

    def test_custom_embedder_is_used_for_add_and_query(self):
        stub = _StubEmbedder()
        store = VectorStore(":memory:", embedder=stub)
        try:
            store.add("the memory limit fact", {"source": "t"})
            store.add("an unrelated sentence", {"source": "t"})
            self.assertIn("the memory limit fact", stub.calls)

            hits = store.query("what is the limit?", top_k=2)
            self.assertTrue(hits)
            self.assertIn("limit", hits[0]["text"])

            stored = json.loads(
                store.conn.execute(
                    "SELECT embedding FROM knowledge ORDER BY id"
                ).fetchone()[0]
            )
            self.assertEqual(len(stored), stub.dimensions)
        finally:
            store.close()

    def test_store_does_not_hardcode_module_embed(self):
        """A store given a stub must never fall back to the default backend."""
        stub = _StubEmbedder()
        store = VectorStore(":memory:", embedder=stub)
        try:
            store.add("some knowledge about the limit", {"source": "t"})
            store.query("limit")
            self.assertGreaterEqual(len(stub.calls), 2)
        finally:
            store.close()


class TestBackendSanity(unittest.TestCase):
    def test_deterministic_backend_ranks_related_text_higher(self):
        embedder = DeterministicEmbedder(load_rules())
        probe = embedder.embed("sqlite database storage")
        near = embedder.embed("the sqlite database stores entries")
        far = embedder.embed("breakfast tastes better with coffee")
        self.assertGreater(cosine(probe, near), cosine(probe, far))


if __name__ == "__main__":
    unittest.main()
