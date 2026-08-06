"""Vector store: deterministic embeddings and cosine ranking."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.rules import Embedder, cosine, embed, load_rules  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402


class TestEmbedding(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()

    def test_embedding_is_deterministic(self):
        a = embed("Faisal prefers concise answers", self.rules)
        b = embed("Faisal prefers concise answers", self.rules)
        self.assertEqual(a, b)

    def test_embedding_is_normalised(self):
        vector = embed("the memory char limit is 2200", self.rules)
        norm = sum(v * v for v in vector) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=6)

    def test_empty_text_gives_zero_vector(self):
        self.assertEqual(set(embed("", self.rules)), {0.0})

    def test_cosine_bounds_and_mismatch(self):
        vector = embed("vector store cosine similarity", self.rules)
        self.assertAlmostEqual(cosine(vector, vector), 1.0, places=6)
        self.assertEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertEqual(cosine([1.0], [1.0, 2.0]), 0.0)  # length mismatch
        self.assertEqual(cosine([0.0, 0.0], [1.0, 1.0]), 0.0)  # zero vector

    def test_related_text_scores_above_unrelated(self):
        probe = embed("sqlite database storage", self.rules)
        near = embed("the sqlite database stores entries", self.rules)
        far = embed("breakfast tastes better with coffee", self.rules)
        self.assertGreater(cosine(probe, near), cosine(probe, far))


class TestVectorStore(unittest.TestCase):
    def setUp(self):
        self.store = VectorStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_add_and_count(self):
        row_id = self.store.add("Faisal is the final approval authority", {"source": "t"})
        self.assertIsNotNone(row_id)
        self.assertEqual(self.store.count(), 1)

    def test_empty_text_is_rejected(self):
        self.assertIsNone(self.store.add("   ", {"source": "t"}))
        self.assertEqual(self.store.count(), 0)

    def test_duplicate_is_ignored(self):
        self.store.add("same fact", {"source": "log"})
        self.assertIsNone(self.store.add("same fact", {"source": "log"}))
        self.assertEqual(self.store.count(), 1)

    def test_query_ranks_most_similar_first(self):
        """Acceptance: the most semantically similar entry ranks #1."""
        self.store.add("The memory char limit is 2200 characters", {"source": "a"})
        self.store.add("Faisal prefers replies in Bahasa Melayu", {"source": "b"})
        self.store.add("Coffee is brewed every morning at nine", {"source": "c"})

        hits = self.store.query("what is the memory character limit?", top_k=3)

        self.assertTrue(hits, "expected at least one hit")
        self.assertIn("2200", hits[0]["text"])
        scores = [h["score"] for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_query_respects_top_k(self):
        for i in range(6):
            self.store.add("memory limit fact number %d" % i, {"source": "s"})
        self.assertEqual(len(self.store.query("memory limit fact", top_k=2)), 2)

    def test_query_on_empty_store(self):
        self.assertEqual(self.store.query("anything"), [])

    def test_metadata_roundtrip(self):
        self.store.add("fact", {"source": "log.jsonl", "ts": "2026-08-06T00:00:00Z", "role": "user"})
        entry = self.store.all_entries()[0]
        self.assertEqual(entry["source"], "log.jsonl")
        self.assertEqual(entry["type"], "knowledge")
        self.assertEqual(entry["meta"].get("role"), "user")


class _FakeEmbedder(Embedder):
    """A deliberately different-dimension backend for the mixing regression."""

    name = "fake-3d"

    def __init__(self, dim=3):
        self._dim = dim

    @property
    def dimensions(self):
        return self._dim

    def embed(self, text):
        vector = [0.0] * self._dim
        for ch in text:
            vector[ord(ch) % self._dim] += 1.0
        return vector


class TestBackendMismatchIsNotSilent(unittest.TestCase):
    """Regression for P1: a dimension/backend flip must not silently mix.

    Reproduces Faisal's finding - deterministic (512-d) rows already in the db,
    then a different-dimension backend queries the same db. Old memory must not
    be silently dropped from ranking, nor mixed: the store must skip mismatched
    rows and the curator must fail loud via check_compatibility().
    """

    def setUp(self):
        self.rules = load_rules()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_query_skips_dimension_mismatch_and_keeps_new(self):
        # Seed with the real deterministic backend (512-d).
        seed = VectorStore(self.db_path, rules=self.rules)
        seed.add("the memory char limit is 2200", {"source": "s"})
        seed.close()

        # Reopen with a 3-d backend over the SAME file.
        other = VectorStore(self.db_path, rules=self.rules, embedder=_FakeEmbedder(3))
        other.add("a brand new fact", {"source": "s"})
        hits = other.query("a brand new fact", top_k=10)

        texts = [h["text"] for h in hits]
        self.assertIn("a brand new fact", texts, "new-row must rank")
        self.assertNotIn(
            "the memory char limit is 2200",
            texts,
            "old 512-d row must be skipped, not silently mixed/dropped",
        )
        other.close()

    def test_check_compatibility_reports_mismatch(self):
        seed = VectorStore(self.db_path, rules=self.rules)
        seed.add("legacy deterministic row", {"source": "s"})
        seed.close()

        other = VectorStore(self.db_path, rules=self.rules, embedder=_FakeEmbedder(3))
        report = other.check_compatibility()
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["mismatched"], 1)
        self.assertEqual(report["matched"], 0)
        self.assertEqual(report["active_dim"], 3)
        with self.assertRaises(ValueError):
            other.check_compatibility(raise_on_mismatch=True)
        other.close()

    def test_rows_are_tagged_with_backend_and_dim(self):
        store = VectorStore(self.db_path, rules=self.rules)
        store.add("tagged fact", {"source": "s"})
        store.close()
        conn = __import__("sqlite3").connect(self.db_path)
        row = conn.execute("SELECT embed_meta FROM knowledge").fetchone()
        conn.close()
        import json as _json

        tag = _json.loads(row[0])
        self.assertEqual(tag["backend"], "deterministic")
        self.assertEqual(tag["dim"], store_dims(self.rules))


def store_dims(rules):
    return int(rules["embedding"]["dimensions"])


if __name__ == "__main__":
    unittest.main()
