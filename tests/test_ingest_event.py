"""Curator.ingest_event adapter + additive provenance/status migrations."""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.graph_store import GraphStore  # noqa: E402
from core.memory_curator import Curator  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402


class IngestEventTestCase(unittest.TestCase):
    def setUp(self):
        self.vector = VectorStore(":memory:")
        self.graph = GraphStore(":memory:")
        self.curator = Curator(vector_store=self.vector, graph_store=self.graph)

    def tearDown(self):
        self.vector.close()
        self.graph.close()


class TestIngestEvent(IngestEventTestCase):
    def _event(self, **extra):
        event = {
            "event_id": "evt_test_1",
            "request_id": "req-1",
            "agent": "lekiu",
            "project": "jebat-cortex",
            "result_summary": "Implemented the durable queue",
            "status": "completed",
            "timestamp": "2026-08-06T12:00:00+00:00",
            "decisions": [{"text": "Use SQLite for the queue", "status": "approved"}],
            "lessons": [{"text": "Commit before returning 202"}],
            "open_tasks": [{"title": "Write the consolidation worker", "status": "open"}],
            "artefacts": [{"path_or_ref": "workers/queue.py", "kind": "file"}],
        }
        event.update(extra)
        return event

    def test_counts_each_memory_type(self):
        report = self.curator.ingest_event(self._event())
        counts = report["counts"]
        self.assertEqual(counts["decisions"], 1)
        self.assertEqual(counts["lessons"], 1)
        self.assertEqual(counts["open_tasks"], 1)
        self.assertEqual(counts["artefacts"], 1)
        self.assertEqual(counts["summary"], 1)

    def test_decision_keeps_its_declared_status(self):
        self.curator.ingest_event(
            self._event(decisions=[{"text": "Maybe adopt Kafka", "status": "proposed"}])
        )
        approved = self.vector.search(filters={"memory_type": "decision", "status": "approved"})
        proposed = self.vector.search(filters={"memory_type": "decision", "status": "proposed"})
        self.assertNotIn("Maybe adopt Kafka", [r["text"] for r in approved])
        self.assertIn("Maybe adopt Kafka", [r["text"] for r in proposed])

    def test_provenance_points_at_the_event(self):
        self.curator.ingest_event(self._event())
        row = self.vector.search(filters={"memory_type": "decision"})[0]
        self.assertEqual(row["source_type"], "agent_event")
        self.assertEqual(row["source_id"], "evt_test_1")
        self.assertEqual(row["project"], "jebat-cortex")

    def test_artefact_becomes_a_graph_edge(self):
        self.curator.ingest_event(self._event())
        edges = self.graph.related("workers/queue.py")
        self.assertTrue(edges)
        self.assertEqual(edges[0]["rel"], "produced")

    def test_failed_event_summary_is_not_approved(self):
        self.curator.ingest_event(self._event(status="failed"))
        approved_texts = [
            r["text"] for r in self.vector.search(filters={"status": "approved"}, limit=50)
        ]
        self.assertNotIn("Implemented the durable queue", approved_texts)

    def test_empty_event_is_harmless(self):
        report = self.curator.ingest_event({"event_id": "evt_empty", "request_id": "r"})
        self.assertEqual(sum(report["counts"].values()), 0)

    def test_blank_entries_are_skipped(self):
        report = self.curator.ingest_event(
            self._event(decisions=[{"text": "   "}], lessons=[{"text": ""}])
        )
        self.assertEqual(report["counts"]["decisions"], 0)
        self.assertEqual(report["counts"]["lessons"], 0)

    def test_same_text_from_two_events_is_not_silently_dropped(self):
        """Regression: identical decision text from a second event vanished.

        The vector store fingerprints on "source::text". ingest_event used a
        constant source of "agent_event", so the second event's decision was
        swallowed by INSERT OR IGNORE while the worker reported success -
        silent memory loss. The source is now scoped per event.
        """
        self.curator.ingest_event(
            {
                "event_id": "evt_a",
                "request_id": "ra",
                "project": "project-alpha",
                "decisions": [{"text": "Use SQLite for the durable queue", "status": "approved"}],
            }
        )
        report = self.curator.ingest_event(
            {
                "event_id": "evt_b",
                "request_id": "rb",
                "project": "project-beta",
                "decisions": [{"text": "Use SQLite for the durable queue", "status": "proposed"}],
            }
        )

        self.assertEqual(report["counts"]["decisions"], 1, "second event must store its decision")
        beta = self.vector.search(filters={"project": "project-beta", "memory_type": "decision"})
        self.assertEqual(len(beta), 1)
        self.assertEqual(beta[0]["status"], "proposed", "each project keeps its own status")

        alpha = self.vector.search(filters={"project": "project-alpha", "memory_type": "decision"})
        self.assertEqual(alpha[0]["status"], "approved")

    def test_duplicate_within_one_event_is_reported_not_hidden(self):
        """A genuine duplicate is still suppressed, but never silently."""
        report = self.curator.ingest_event(
            {
                "event_id": "evt_dup",
                "request_id": "rd",
                "decisions": [
                    {"text": "Exactly the same decision", "status": "approved"},
                    {"text": "Exactly the same decision", "status": "approved"},
                ],
            }
        )
        self.assertEqual(report["counts"]["decisions"], 1)
        self.assertEqual(report["skipped_count"], 1, "the dropped item must be reported")
        self.assertEqual(report["skipped"][0]["reason"], "duplicate_fingerprint")

    def test_clean_event_reports_nothing_skipped(self):
        report = self.curator.ingest_event(self._event())
        self.assertEqual(report["skipped_count"], 0)


class TestAdditiveMigration(unittest.TestCase):
    """An old database must open and gain the new columns, not be rejected."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cortex-migrate-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_legacy_vector_db_is_migrated(self):
        path = os.path.join(self.tmp, "old_vector.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                embedding TEXT NOT NULL,
                source TEXT,
                ts TEXT,
                type TEXT NOT NULL DEFAULT 'knowledge',
                meta TEXT,
                fingerprint TEXT UNIQUE
            );
            """
        )
        conn.execute(
            "INSERT INTO knowledge (text, embedding, source) VALUES ('old row', '[0.0]', 'legacy')"
        )
        conn.commit()
        conn.close()

        store = VectorStore(path)
        self.addCleanup(store.close)
        columns = {row[1] for row in store.conn.execute("PRAGMA table_info(knowledge)")}
        for expected in ("status", "source_type", "project", "confidence", "embed_meta"):
            self.assertIn(expected, columns, "migration must add %s" % expected)

        # Pre-existing rows predate the status model: treat them as approved,
        # not as an alarming NULL that disappears from every query.
        self.assertEqual(store.all_entries()[0]["status"], "approved")

    def test_legacy_graph_db_is_migrated(self):
        path = os.path.join(self.tmp, "old_graph.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'entity',
                meta TEXT,
                ts TEXT,
                UNIQUE(label, kind)
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                src INTEGER NOT NULL REFERENCES nodes(id),
                dst INTEGER NOT NULL REFERENCES nodes(id),
                rel TEXT NOT NULL,
                source TEXT,
                ts TEXT,
                meta TEXT,
                UNIQUE(src, dst, rel)
            );
            """
        )
        conn.commit()
        conn.close()

        store = GraphStore(path)
        self.addCleanup(store.close)
        edge_cols = {row[1] for row in store.conn.execute("PRAGMA table_info(edges)")}
        self.assertIn("status", edge_cols)
        self.assertIn("project", edge_cols)

    def test_superseded_memory_is_excluded_but_retained(self):
        store = VectorStore(":memory:")
        self.addCleanup(store.close)
        row_id = store.add("an old decision", {"source": "t", "type": "decision"})
        store.set_status(row_id, "superseded")

        self.assertEqual(store.search(filters={"memory_type": "decision"}), [])
        kept = store.search(filters={"memory_type": "decision", "status": "superseded"})
        self.assertEqual(len(kept), 1, "superseded memory must remain queryable")


if __name__ == "__main__":
    unittest.main()
