"""Graph store: node/edge writes, keyword query ranking, ripgrep fallback."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.graph_store import GraphStore, rg_available, ripgrep_fallback  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestGraphStore(unittest.TestCase):
    def setUp(self):
        self.store = GraphStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_add_node_returns_id_and_dedupes(self):
        first = self.store.add_node("core/vector_store.py", kind="file")
        second = self.store.add_node("core/vector_store.py", kind="file")
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertEqual(self.store.count_nodes(), 1)

    def test_empty_label_rejected(self):
        self.assertIsNone(self.store.add_node("  "))
        self.assertEqual(self.store.count_nodes(), 0)

    def test_add_edge_creates_endpoints(self):
        edge_id = self.store.add_edge(
            "core/context_builder.py", "imports", "core/vector_store.py"
        )
        self.assertIsNotNone(edge_id)
        self.assertEqual(self.store.count_nodes(), 2)
        self.assertEqual(self.store.count_edges(), 1)

    def test_duplicate_edge_is_not_doubled(self):
        self.store.add_edge("a.py", "imports", "b.py")
        self.store.add_edge("a.py", "imports", "b.py")
        self.assertEqual(self.store.count_edges(), 1)

    def test_empty_relation_rejected(self):
        self.assertIsNone(self.store.add_edge("a.py", "", "b.py"))
        self.assertEqual(self.store.count_edges(), 0)

    def test_query_finds_and_ranks_relevant_edges(self):
        self.store.add_edge("core/context_builder.py", "imports", "core/vector_store.py")
        self.store.add_edge("run_cortex.sh", "runs", "core/memory_curator.py")
        self.store.add_edge("kitchen", "contains", "kettle")

        hits = self.store.query("vector_store", top_k=5)

        self.assertTrue(hits, "expected a graph hit for vector_store")
        self.assertIn("vector_store", hits[0]["text"])
        self.assertEqual(hits[0]["type"], "experience")
        scores = [h["score"] for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertNotIn("kettle", " ".join(h["text"] for h in hits))

    def test_query_respects_top_k_and_empty_probe(self):
        for i in range(5):
            self.store.add_edge("mod%d.py" % i, "imports", "shared_module.py")
        self.assertEqual(len(self.store.query("shared_module", top_k=2)), 2)
        self.assertEqual(self.store.query(""), [])

    def test_neighbors_returns_both_directions(self):
        self.store.add_edge("a.py", "imports", "b.py")
        self.store.add_edge("c.py", "imports", "a.py")
        self.assertEqual(len(self.store.neighbors("a.py")), 2)


class TestRipgrepFallback(unittest.TestCase):
    def test_empty_keyword_returns_empty(self):
        self.assertEqual(ripgrep_fallback(""), [])

    def test_missing_directory_returns_empty(self):
        self.assertEqual(ripgrep_fallback("anything", repo="/nonexistent/path/xyz"), [])

    def test_degrades_gracefully_without_rg(self):
        """Contract: no rg on PATH -> [] , never an exception."""
        import core.graph_store as gs

        original = gs.shutil.which
        gs.shutil.which = lambda name: None
        try:
            self.assertEqual(ripgrep_fallback("VectorStore", repo=REPO_ROOT), [])
            self.assertFalse(rg_available())
        finally:
            gs.shutil.which = original

    @unittest.skipUnless(rg_available(), "ripgrep not installed on this host")
    def test_finds_real_references(self):
        hits = ripgrep_fallback("class VectorStore", repo=REPO_ROOT, top_k=5)
        self.assertTrue(hits, "expected to find 'class VectorStore' in the repo")
        hit = hits[0]
        self.assertIn("file", hit)
        self.assertIsInstance(hit["line"], int)
        self.assertEqual(hit["origin"], "ripgrep")
        self.assertFalse(os.path.isabs(hit["file"]), "file path should be repo-relative")


if __name__ == "__main__":
    unittest.main()
