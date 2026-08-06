"""Durable event queue: idempotency, restart recovery, bounded retry, depth.

These are the properties EVENT_SCHEMA.md section 2 promises, so each is tested
against real on-disk SQLite (not :memory:) where durability is the point.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.queue import (  # noqa: E402
    STATUS_DEAD,
    STATUS_DONE,
    STATUS_PROCESSING,
    STATUS_QUEUED,
    EventQueue,
    backoff_seconds,
)


def _event(request_id="req-1", **extra):
    payload = {
        "request_id": request_id,
        "agent": "lekiu",
        "result_summary": "did the thing",
        "status": "completed",
    }
    payload.update(extra)
    return payload


class QueueTestCase(unittest.TestCase):
    """On-disk queue so restart/durability behaviour is real."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cortex-queue-")
        self.db_path = os.path.join(self.tmp, "event_queue.db")
        self.queue = EventQueue(self.db_path)

    def tearDown(self):
        self.queue.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestEnqueue(QueueTestCase):
    def test_enqueue_returns_event_id_and_queues(self):
        result = self.queue.enqueue(_event())
        self.assertTrue(result["queued"])
        self.assertFalse(result["duplicate"])
        self.assertTrue(result["event_id"].startswith("evt_"))
        self.assertEqual(self.queue.depth(), 1)

    def test_request_id_is_required(self):
        with self.assertRaises(ValueError):
            self.queue.enqueue({"agent": "lekiu"})

    def test_payload_roundtrips(self):
        self.queue.enqueue(_event(project="jebat-cortex"))
        row = self.queue.by_request_id("req-1")
        self.assertEqual(row["payload"]["project"], "jebat-cortex")


class TestIdempotency(QueueTestCase):
    """A duplicate request_id must never enqueue or ingest twice."""

    def test_duplicate_request_id_is_not_requeued(self):
        first = self.queue.enqueue(_event())
        second = self.queue.enqueue(_event())

        self.assertTrue(first["queued"])
        self.assertFalse(second["queued"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(self.queue.depth(), 1, "duplicate must not add a second row")

    def test_duplicate_after_processing_still_reports_original(self):
        first = self.queue.enqueue(_event())
        claimed = self.queue.claim()
        self.queue.ack(claimed["event_id"])

        again = self.queue.enqueue(_event())
        self.assertFalse(again["queued"])
        self.assertEqual(again["event_id"], first["event_id"])
        self.assertEqual(again["status"], STATUS_DONE)

    def test_different_request_ids_are_independent(self):
        self.queue.enqueue(_event("req-a"))
        self.queue.enqueue(_event("req-b"))
        self.assertEqual(self.queue.depth(), 2)


class TestClaimAck(QueueTestCase):
    def test_claim_marks_processing_and_increments_attempts(self):
        self.queue.enqueue(_event())
        claimed = self.queue.claim()
        self.assertEqual(claimed["status"], STATUS_PROCESSING)
        self.assertEqual(claimed["attempts"], 1)

    def test_claim_returns_none_when_empty(self):
        self.assertIsNone(self.queue.claim())

    def test_claimed_row_is_not_claimed_twice(self):
        self.queue.enqueue(_event())
        self.assertIsNotNone(self.queue.claim())
        self.assertIsNone(self.queue.claim(), "a processing row must not be re-claimed")

    def test_ack_marks_done_and_clears_depth(self):
        self.queue.enqueue(_event())
        claimed = self.queue.claim()
        self.assertTrue(self.queue.ack(claimed["event_id"]))
        self.assertEqual(self.queue.depth(), 0)
        self.assertEqual(self.queue.by_event_id(claimed["event_id"])["status"], STATUS_DONE)


class TestRestartRecovery(QueueTestCase):
    """A crashed worker must not strand an event in 'processing'."""

    def test_processing_rows_reset_to_queued_on_boot(self):
        self.queue.enqueue(_event())
        claimed = self.queue.claim()
        self.assertEqual(claimed["status"], STATUS_PROCESSING)

        # Simulate a crash: close without ack, reopen the same file.
        self.queue.close()
        reopened = EventQueue(self.db_path)
        self.addCleanup(reopened.close)

        self.assertEqual(reopened.by_event_id(claimed["event_id"])["status"], STATUS_PROCESSING)
        reset = reopened.reset_stale_processing()
        self.assertEqual(reset, 1)
        self.assertEqual(reopened.by_event_id(claimed["event_id"])["status"], STATUS_QUEUED)
        self.assertIsNotNone(reopened.claim(), "recovered event must be claimable again")
        self.queue = reopened  # let tearDown close the live handle

    def test_events_survive_process_restart(self):
        self.queue.enqueue(_event("persist-me"))
        self.queue.close()

        reopened = EventQueue(self.db_path)
        self.addCleanup(reopened.close)
        self.assertIsNotNone(reopened.by_request_id("persist-me"))
        self.queue = reopened


class TestBoundedRetry(QueueTestCase):
    def test_backoff_is_exponential_and_capped(self):
        self.assertEqual(backoff_seconds(1), 2)
        self.assertEqual(backoff_seconds(2), 4)
        self.assertEqual(backoff_seconds(3), 8)
        self.assertLessEqual(backoff_seconds(50), 300, "backoff must stay capped")

    def test_failure_requeues_with_backoff(self):
        self.queue.enqueue(_event())
        claimed = self.queue.claim()
        outcome = self.queue.fail(claimed["event_id"], "boom")

        self.assertEqual(outcome["status"], STATUS_QUEUED)
        self.assertIsNotNone(outcome["next_attempt"])
        # Scheduled in the future, so it is not immediately re-claimable.
        self.assertIsNone(self.queue.claim())

    def test_dead_letter_after_max_attempts(self):
        queue = EventQueue(os.path.join(self.tmp, "dead.db"), max_attempts=2)
        self.addCleanup(queue.close)
        queue.enqueue(_event("dies"))

        for _ in range(2):
            row = queue.by_request_id("dies")
            queue.conn.execute(
                "UPDATE event_queue SET status = ?, next_attempt = NULL WHERE event_id = ?",
                (STATUS_QUEUED, row["event_id"]),
            )
            claimed = queue.claim()
            self.assertIsNotNone(claimed)
            outcome = queue.fail(claimed["event_id"], "still broken")

        self.assertEqual(outcome["status"], STATUS_DEAD)
        self.assertEqual(queue.by_request_id("dies")["status"], STATUS_DEAD)

    def test_dead_letters_are_queryable_not_dropped(self):
        queue = EventQueue(os.path.join(self.tmp, "dl.db"), max_attempts=1)
        self.addCleanup(queue.close)
        queue.enqueue(_event("dl"))
        claimed = queue.claim()
        queue.fail(claimed["event_id"], "fatal")

        letters = queue.dead_letters()
        self.assertEqual(len(letters), 1)
        self.assertEqual(letters[0]["request_id"], "dl")


class TestPoisonEventCrashLoop(QueueTestCase):
    """Regression: an event that hard-crashes the worker retried forever.

    claim() bumps attempts, but max_attempts is only evaluated in fail(). A
    worker killed mid-event (segfault/OOM/SIGKILL) never calls fail(), so boot
    recovery requeued the row indefinitely and the poison event took the worker
    down on every restart.
    """

    def test_crash_loop_eventually_dead_letters(self):
        queue = EventQueue(os.path.join(self.tmp, "poison.db"), max_attempts=5)
        self.addCleanup(queue.close)
        queue.enqueue(_event("poison"))

        for _ in range(10):
            if queue.claim() is None:
                break
            # Worker dies here: no ack, no fail - only boot recovery runs.
            queue.reset_stale_processing()

        row = queue.by_request_id("poison")
        self.assertEqual(row["status"], STATUS_DEAD, "poison event must dead-letter")
        self.assertLessEqual(row["attempts"], 5 + 1)
        self.assertIsNotNone(row["last_error"])

    def test_recovery_still_requeues_under_the_attempt_ceiling(self):
        queue = EventQueue(os.path.join(self.tmp, "ok.db"), max_attempts=5)
        self.addCleanup(queue.close)
        queue.enqueue(_event("recoverable"))

        queue.claim()  # attempts -> 1
        self.assertEqual(queue.reset_stale_processing(), 1)
        self.assertEqual(queue.by_request_id("recoverable")["status"], STATUS_QUEUED)
        self.assertIsNotNone(queue.claim(), "a healthy retry must still be claimable")


class TestPayloadMismatch(QueueTestCase):
    """Regression: a reused request_id with different content was dropped silently."""

    def test_divergent_payload_is_flagged(self):
        self.queue.enqueue(_event("dup", result_summary="FIRST"))
        outcome = self.queue.enqueue(_event("dup", result_summary="SECOND-DIFFERENT"))

        self.assertFalse(outcome["queued"])
        self.assertTrue(outcome["duplicate"])
        self.assertTrue(outcome.get("payload_mismatch"), "divergent content must be reported")
        self.assertIn("NOT stored", outcome["warning"])

    def test_identical_resend_is_not_flagged(self):
        self.queue.enqueue(_event("dup"))
        outcome = self.queue.enqueue(_event("dup"))
        self.assertFalse(outcome.get("payload_mismatch", False))

    def test_volatile_fields_do_not_trigger_a_mismatch(self):
        """A retry with a fresh timestamp/event_id is the same event."""
        self.queue.enqueue(_event("dup", timestamp="2026-08-06T10:00:00+00:00"))
        outcome = self.queue.enqueue(_event("dup", timestamp="2026-08-06T11:30:00+00:00"))
        self.assertFalse(outcome.get("payload_mismatch", False))

    def test_first_payload_is_preserved(self):
        self.queue.enqueue(_event("dup", result_summary="FIRST"))
        self.queue.enqueue(_event("dup", result_summary="SECOND"))
        self.assertEqual(self.queue.by_request_id("dup")["payload"]["result_summary"], "FIRST")


class TestObservability(QueueTestCase):
    def test_depth_counts_only_outstanding_work(self):
        self.queue.enqueue(_event("a"))
        self.queue.enqueue(_event("b"))
        self.assertEqual(self.queue.depth(), 2)

        claimed = self.queue.claim()
        self.assertEqual(self.queue.depth(), 2, "processing still counts as outstanding")
        self.queue.ack(claimed["event_id"])
        self.assertEqual(self.queue.depth(), 1)

    def test_stats_reports_per_status_counts(self):
        self.queue.enqueue(_event("a"))
        self.queue.enqueue(_event("b"))
        self.queue.ack(self.queue.claim()["event_id"])

        stats = self.queue.stats()
        self.assertEqual(stats[STATUS_DONE], 1)
        self.assertEqual(stats[STATUS_QUEUED], 1)
        self.assertEqual(stats["total"], 2)

    def test_last_done_at_tracks_ingestion(self):
        self.assertIsNone(self.queue.last_done_at())
        self.queue.enqueue(_event())
        self.queue.ack(self.queue.claim()["event_id"])
        self.assertIsNotNone(self.queue.last_done_at())


if __name__ == "__main__":
    unittest.main()
