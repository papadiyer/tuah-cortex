"""POST /v1/events/postflight: durability, idempotency, worker round-trip.

The end-to-end test is the important one: postflight -> queue -> worker ->
searchable approved decision, which is the acceptance criterion in the task.
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


def _event(request_id="evt-req-1", **extra):
    body = {
        "request_id": request_id,
        "actor": "faisal",
        "agent": "lekiu",
        "project": "jebat-cortex",
        "prompt": "build the queue",
        "result_summary": "Durable SQLite queue implemented and tested",
        "status": "completed",
        "decisions": [
            {"text": "Use a SQLite durable queue", "status": "approved", "project": "jebat-cortex"}
        ],
        "lessons": [{"text": "Commit before returning 202", "project": "jebat-cortex"}],
        "open_tasks": [{"title": "Add consolidation worker", "status": "open"}],
        "artefacts": [{"path_or_ref": "workers/queue.py", "kind": "file"}],
    }
    body.update(extra)
    return body


class EventsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cortex-evt-")
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

    def _post(self, body):
        return self.app.dispatch("POST", "/v1/events/postflight", body)

    def _worker(self):
        curator = Curator(rules=self.service.rules, vector_store=self.vector, graph_store=self.graph)
        return IngestWorker(queue=self.queue, curator=curator, rules=self.service.rules)


class TestPostflight(EventsTestCase):
    def test_accepts_event_with_202(self):
        status, body = self._post(_event())
        self.assertEqual(status, 202)
        self.assertTrue(body["queued"])
        self.assertTrue(body["event_id"].startswith("evt_"))

    def test_event_is_durable_before_response(self):
        """The row must exist in SQLite by the time 202 is returned."""
        _, body = self._post(_event())
        stored = self.queue.by_event_id(body["event_id"])
        self.assertIsNotNone(stored, "event must be persisted before 202")
        self.assertEqual(stored["status"], "queued")

    def test_request_id_is_required(self):
        status, body = self._post({"result_summary": "no id"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")

    def test_invalid_status_is_rejected(self):
        status, _ = self._post(_event(status="maybe"))
        self.assertEqual(status, 400)

    def test_invalid_decision_status_is_rejected(self):
        status, _ = self._post(_event(decisions=[{"text": "x", "status": "sort-of"}]))
        self.assertEqual(status, 400)

    def test_degraded_queue_returns_503_not_background_job(self):
        self.service.queue = None
        status, body = self._post(_event())
        self.assertEqual(status, 503)
        self.assertFalse(body["queued"])
        self.assertEqual(body["memory_status"], "degraded")


class TestIdempotency(EventsTestCase):
    def test_second_post_with_same_request_id_is_idempotent(self):
        status1, first = self._post(_event())
        status2, second = self._post(_event())

        self.assertEqual(status1, 202)
        self.assertEqual(status2, 202)
        self.assertTrue(first["queued"])
        self.assertFalse(second["queued"], "duplicate must report queued:false")
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(self.queue.depth(), 1, "no duplicate row")

    def test_duplicate_does_not_double_ingest(self):
        self._post(_event())
        worker = self._worker()
        worker.drain()
        count_after_first = self.vector.count()

        self._post(_event())
        worker.drain()
        self.assertEqual(
            self.vector.count(), count_after_first, "duplicate event must not re-ingest"
        )


class TestWorkerRoundTrip(EventsTestCase):
    """postflight -> queue -> worker -> retrievable decision.

    NOTE (Task4 / P1-HIGH-2): these two tests previously asserted that a
    decision submitted through postflight with status='approved' came back as
    approved. That WAS the vulnerability - postflight is unauthenticated, so any
    local caller could mint Tier 1 authority. The round-trip assertion is kept
    (postflight really must reach memory and stay searchable with provenance);
    only the status expectation changed to 'proposed', which is what an
    unauthenticated submission is now allowed to produce.
    """

    def test_decision_becomes_searchable_after_worker_runs(self):
        self._post(_event())
        summary = self._worker().drain()
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["failed"], 0)

        status, body = self.app.dispatch(
            "POST",
            "/v1/memory/search",
            {"filters": {"memory_type": "decision", "status": "proposed", "project": "jebat-cortex"}},
        )
        self.assertEqual(status, 200)
        self.assertIn("Use a SQLite durable queue", [r["text"] for r in body["results"]])

        # ...and it must NOT be retrievable as approved authority.
        _, approved = self.app.dispatch(
            "POST",
            "/v1/memory/search",
            {"filters": {"memory_type": "decision", "status": "approved", "project": "jebat-cortex"}},
        )
        self.assertNotIn("Use a SQLite durable queue", [r["text"] for r in approved["results"]])

    def test_ingested_decision_carries_event_provenance(self):
        self._post(_event())
        self._worker().drain()
        _, body = self.app.dispatch(
            "POST", "/v1/memory/search", {"filters": {"memory_type": "decision", "status": "proposed"}}
        )
        provenance = body["results"][0]["provenance"]
        self.assertEqual(provenance["source_type"], "agent_event")
        self.assertTrue(str(provenance["source_id"]).startswith("evt_"))

    def test_proposed_decision_is_not_stored_as_approved(self):
        self._post(
            _event(
                "proposal-1",
                decisions=[{"text": "Perhaps adopt Kafka", "status": "proposed"}],
            )
        )
        self._worker().drain()

        _, approved = self.app.dispatch(
            "POST", "/v1/memory/search", {"filters": {"memory_type": "decision", "status": "approved"}}
        )
        self.assertNotIn("Perhaps adopt Kafka", [r["text"] for r in approved["results"]])

        _, proposed = self.app.dispatch(
            "POST", "/v1/memory/search", {"filters": {"memory_type": "decision", "status": "proposed"}}
        )
        self.assertIn("Perhaps adopt Kafka", [r["text"] for r in proposed["results"]])

    def test_artefacts_land_in_the_experience_graph(self):
        self._post(_event())
        self._worker().drain()
        self.assertTrue(self.graph.related("workers/queue.py"))

    def test_queue_depth_returns_to_zero(self):
        self._post(_event())
        self._worker().drain()
        self.assertEqual(self.queue.depth(), 0)
        _, health = self.app.dispatch("GET", "/v1/health")
        self.assertEqual(health["queue_depth"], 0)
        self.assertIsNotNone(health["last_ingestion"])

    def test_worker_failure_retries_then_dead_letters(self):
        class _Boom:
            def ingest_event(self, event):
                raise RuntimeError("ingest exploded")

            def close(self):
                pass

        queue = EventQueue(os.path.join(self.tmp, "boom.db"), max_attempts=2)
        self.addCleanup(queue.close)
        queue.enqueue({"request_id": "boom-1"})

        worker = IngestWorker(queue=queue, curator=_Boom(), rules=self.service.rules)
        result = worker.process_one()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "queued", "first failure retries")

        # Clear the backoff so the retry is immediately due.
        queue.conn.execute("UPDATE event_queue SET next_attempt = NULL")
        result = worker.process_one()
        self.assertEqual(result["status"], "dead", "second failure dead-letters")
        self.assertEqual(len(queue.dead_letters()), 1)


if __name__ == "__main__":
    unittest.main()
