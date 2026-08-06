"""Memory curator: classification, segmentation, relation extraction, ingest."""

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.graph_store import GraphStore  # noqa: E402
from core.memory_curator import Curator, _RELATION_CUE_WORDS, read_log  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "tiny_conversation.jsonl")


class TestClassification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.curator = Curator(vector_store=VectorStore(":memory:"), graph_store=GraphStore(":memory:"))

    def test_code_and_paths_classify_as_experience(self):
        for text in [
            "core/context_builder.py imports core/vector_store.py",
            "The repo structure puts every module under core/",
            "from core.rules import embed",
            "run the test script in tests/test_vector_store.py",
        ]:
            self.assertEqual(self.curator.classify(text), "experience", text)

    def test_preferences_and_config_classify_as_knowledge(self):
        for text in [
            "Faisal prefers short and direct answers",
            "My timezone is Asia/Kuala_Lumpur",
            "The rule is to never claim success without evidence",
            "Faisal is the final approval authority",
        ]:
            self.assertEqual(self.curator.classify(text), "knowledge", text)

    def test_scores_are_reported(self):
        scores = self.curator.score_segment("core/vector_store.py imports sqlite3")
        self.assertGreater(scores["experience"], scores["knowledge"])

    def test_ties_fall_back_to_knowledge(self):
        self.assertEqual(self.curator.classify("hello there friend"), "knowledge")


class TestSegmentation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.curator = Curator(vector_store=VectorStore(":memory:"), graph_store=GraphStore(":memory:"))

    def test_splits_sentences_and_drops_short_fragments(self):
        segments = self.curator.segment_message(
            "Faisal prefers concise answers. The memory limit is 2200 characters. ok"
        )
        self.assertEqual(len(segments), 2)
        self.assertTrue(all(len(s) >= 16 for s in segments))

    def test_code_fence_kept_as_one_segment(self):
        segments = self.curator.segment_message(
            "Here is the code:\n```python\ndef build():\n    return 1\n```\n"
        )
        fenced = [s for s in segments if s.startswith("```")]
        self.assertEqual(len(fenced), 1)
        self.assertIn("def build()", fenced[0])

    def test_long_segment_is_chunked(self):
        long_text = "x" * 1500
        segments = self.curator.segment_message(long_text)
        self.assertGreater(len(segments), 1)
        self.assertTrue(all(len(s) <= 600 for s in segments))


class TestRelationExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.curator = Curator(vector_store=VectorStore(":memory:"), graph_store=GraphStore(":memory:"))

    def test_extracts_import_triple(self):
        triples = self.curator.extract_relations(
            "core/context_builder.py imports core/vector_store.py"
        )
        self.assertTrue(triples)
        self.assertEqual(triples[0]["src"], "core/context_builder.py")
        self.assertEqual(triples[0]["rel"], "imports")
        self.assertEqual(triples[0]["dst"], "core/vector_store.py")

    def test_detects_writes_to_relation(self):
        triples = self.curator.extract_relations(
            "core/memory_curator.py writes to data/graph_store.db"
        )
        self.assertTrue(triples)
        self.assertEqual(triples[0]["rel"], "writes_to")

    def test_no_entities_yields_no_triples(self):
        self.assertEqual(self.curator.extract_relations("just some prose here"), [])

    def test_database_files_are_recognised_as_entities(self):
        """Regression: .db targets were missed, collapsing the triple's object."""
        triples = self.curator.extract_relations(
            "core/memory_curator.py writes to data/vector_store.db for knowledge"
        )
        self.assertTrue(triples)
        self.assertEqual(triples[0]["src"], "core/memory_curator.py")
        self.assertEqual(triples[0]["rel"], "writes_to")
        self.assertEqual(triples[0]["dst"], "data/vector_store.db")

    def test_relation_cue_never_becomes_the_object(self):
        """Regression: produced 'memory_curator.py writes_to writes'."""
        triples = self.curator.extract_relations("core/memory_curator.py writes to the store")
        for triple in triples:
            self.assertNotIn(
                triple["dst"],
                {"writes", "write", "imports", "import", "tests", "test"},
                "cue word leaked into the object position: %s" % triple,
            )


class TestLogReading(unittest.TestCase):
    def test_counts_malformed_lines(self):
        messages, malformed = read_log(FIXTURE)
        self.assertEqual(len(messages), 6)
        self.assertEqual(malformed, 1)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            read_log("/nonexistent/log.jsonl")


class TestIngest(unittest.TestCase):
    def test_ingest_routes_into_both_stores(self):
        vector = VectorStore(":memory:")
        graph = GraphStore(":memory:")
        curator = Curator(vector_store=vector, graph_store=graph)
        try:
            report = asyncio.run(curator.ingest(FIXTURE))

            self.assertEqual(report["messages"], 6)
            self.assertEqual(report["malformed_lines"], 1)
            self.assertGreater(report["knowledge_added"], 0, "no knowledge persisted")
            self.assertGreater(report["experience_edges"], 0, "no experience edges persisted")
            self.assertGreater(vector.count(), 0)
            self.assertGreater(graph.count_edges(), 0)

            # The preference fact must be retrievable from the vector store.
            hits = vector.query("what does the user prefer?", top_k=3)
            self.assertTrue(any("prefer" in h["text"].lower() for h in hits))

            # The structural fact must be retrievable from the graph.
            edges = graph.query("context_builder", top_k=3)
            self.assertTrue(edges)
        finally:
            vector.close()
            graph.close()

    def test_ingest_is_idempotent_for_knowledge(self):
        vector = VectorStore(":memory:")
        graph = GraphStore(":memory:")
        curator = Curator(vector_store=vector, graph_store=graph)
        try:
            asyncio.run(curator.ingest(FIXTURE))
            first = vector.count()
            asyncio.run(curator.ingest(FIXTURE))
            self.assertEqual(vector.count(), first, "re-ingest duplicated knowledge")
        finally:
            vector.close()
            graph.close()


class TestNoDegenerateEdges(unittest.TestCase):
    """Regression: a relation cue word must never become an edge's object.

    A real corpus can contain a sentence that literally ends in a cue word
    (e.g. "core/memory_curator.py writes to data/vector_store.db for knowledge
    and data/graph_store.db for experience"). The extractor must route the
    object to a real entity, never to the cue word itself.
    """

    def test_cue_word_never_becomes_object(self):
        import tempfile

        log = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        log.write(
            json.dumps(
                {
                    "role": "assistant",
                    "content": "core/memory_curator.py writes to data/vector_store.db "
                    "for knowledge and data/graph_store.db for experience.",
                }
            )
            + "\n"
        )
        log.close()

        vector = VectorStore(":memory:")
        graph = GraphStore(":memory:")
        curator = Curator(vector_store=vector, graph_store=graph)
        try:
            report = asyncio.run(curator.ingest(log.name))
            self.assertGreater(report["experience_edges"], 0)
            # No edge may point at a relation cue word.
            for cue in _RELATION_CUE_WORDS:
                hits = graph.query(cue, top_k=100)
                for hit in hits:
                    self.assertNotEqual(
                        hit["dst"], cue, "edge object is a relation cue word: %s" % hit
                    )
        finally:
            vector.close()
            graph.close()
            os.unlink(log.name)


if __name__ == "__main__":
    unittest.main()
