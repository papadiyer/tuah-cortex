"""P2-MEDIUM regression: approval must not have a lookup ceiling.

The bug: ``approve_decision`` located its target by scanning
``vector.search(filters={"memory_type": "decision"}, limit=1000)`` newest-first.
Once the store held more than 1,000 decisions, an older ``proposed`` decision
(id=1) was never visited by that window and the endpoint answered 404 for a row
that plainly existed - the decision became permanently unapprovable.

Policy under test:
  * a decision is found by primary key regardless of how many rows follow it;
  * a genuinely absent id is still 404;
  * status transitions (idempotent approve, refuse rejected/superseded) keep
    working through the new path;
  * the P1-HIGH-2 auth controls are untouched - promotion still needs the admin
    token AND a loopback peer.
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
from core.vector_store import VectorStore  # noqa: E402
from workers.queue import EventQueue  # noqa: E402

ADMIN_TOKEN = "test-admin-token-value"
SEEDED = 1001


class DecisionApprovalLookupTest(unittest.TestCase):
    """Seed past the old 1,000-row window and approve from the far end."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cortex-lookup-")
        self.vector = VectorStore(":memory:")
        self.graph = GraphStore(":memory:")
        self.queue = EventQueue(os.path.join(self.tmp, "queue.db"))
        self.service = CortexService(
            vector_store=self.vector,
            graph_store=self.graph,
            queue=self.queue,
            admin_token=ADMIN_TOKEN,
        )
        self.app = CortexApp(service=self.service)

        # 1,001 distinct proposed decisions -> ids 1..1001 in a fresh store.
        # Text must be unique: add() fingerprints exact text and drops dupes.
        for n in range(1, SEEDED + 1):
            self.vector.add(
                "decision number %d: adopt approach %d" % (n, n),
                {"type": "decision", "status": "proposed", "source": "seed"},
            )
        self.assertEqual(self.vector.count(), SEEDED, "seeding did not produce 1,001 rows")

    def tearDown(self):
        self.service.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _approve(self, decision_id, token=ADMIN_TOKEN, peer="127.0.0.1", body=None):
        headers = {"X-Cortex-Admin-Token": token} if token is not None else {}
        return self.app.dispatch(
            "POST",
            "/v1/admin/decision/%s/approve" % decision_id,
            body if body is not None else {},
            headers,
            peer=peer,
        )

    def _status_of(self, decision_id):
        row = self.vector.get_by_id(decision_id)
        return None if row is None else row["status"]

    # -- the reproducer ----------------------------------------------------
    def test_oldest_decision_beyond_the_window_is_approvable(self):
        """id=1 sits 1,000 rows behind the newest - the exact 404 case."""
        self.assertEqual(self._status_of(1), "proposed")

        status, body = self._approve(1, body={"approved_by": "faisal"})

        self.assertEqual(
            status, 200,
            "id=1 returned %d - lookup ceiling regression (P2-MEDIUM)" % status,
        )
        self.assertEqual(body["status"], "approved")
        self.assertEqual(body["previous_status"], "proposed")
        self.assertTrue(body["changed"])
        self.assertEqual(self._status_of(1), "approved")

    def test_middle_and_newest_decisions_are_approvable(self):
        for decision_id in (500, SEEDED):
            with self.subTest(decision_id=decision_id):
                status, body = self._approve(decision_id)
                self.assertEqual(status, 200, "id=%d returned %d" % (decision_id, status))
                self.assertEqual(body["status"], "approved")
                self.assertEqual(self._status_of(decision_id), "approved")

    def test_absent_id_is_still_404(self):
        status, body = self._approve(99999)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    # -- transitions still hold through the new path -----------------------
    def test_already_approved_old_decision_is_idempotent(self):
        self.assertEqual(self._approve(1)[0], 200)
        status, body = self._approve(1)
        self.assertEqual(status, 200)
        self.assertFalse(body["changed"])
        self.assertEqual(body["status"], "approved")

    def test_rejected_old_decision_is_409_not_404(self):
        """Beyond the window a closed decision must report the real reason."""
        self.assertTrue(self.vector.set_status(2, "rejected"))
        status, body = self._approve(2)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "invalid_transition")
        self.assertEqual(self._status_of(2), "rejected")

    def test_superseded_old_decision_is_409(self):
        self.assertTrue(self.vector.set_status(3, "superseded"))
        status, body = self._approve(3)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "invalid_transition")
        self.assertEqual(self._status_of(3), "superseded")

    def test_non_decision_row_is_404(self):
        knowledge_id = self.vector.add(
            "a plain knowledge row, not a decision", {"type": "knowledge"}
        )
        self.assertIsNotNone(knowledge_id)
        status, body = self._approve(knowledge_id)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    # -- P1-HIGH-2 controls must not have been loosened --------------------
    def test_old_decision_still_needs_token_and_loopback(self):
        self.assertEqual(self._approve(1, token=None)[0], 401)
        self.assertEqual(self._approve(1, token="wrong-token")[0], 401)
        self.assertEqual(self._approve(1, peer="10.0.0.7")[0], 403)
        self.assertEqual(
            self._status_of(1), "proposed",
            "an unauthorised caller changed a decision's status",
        )


class VectorStoreGetByIdTest(unittest.TestCase):
    """Unit cover for the canonical id-indexed read."""

    def setUp(self):
        self.vector = VectorStore(":memory:")

    def tearDown(self):
        self.vector.close()

    def test_returns_row_regardless_of_position_or_status(self):
        first = self.vector.add("the very first decision", {"type": "decision", "status": "proposed"})
        for n in range(1200):
            self.vector.add("filler row %d" % n, {"type": "knowledge"})

        row = self.vector.get_by_id(first)
        self.assertIsNotNone(row, "get_by_id lost a row behind 1,200 newer ones")
        self.assertEqual(row["id"], first)
        self.assertEqual(row["status"], "proposed")

        # Statuses hidden from default retrieval are still reachable by id.
        self.vector.set_status(first, "superseded")
        self.assertEqual(self.vector.get_by_id(first)["status"], "superseded")

    def test_expected_type_narrows_the_lookup(self):
        decision_id = self.vector.add("a decision row", {"type": "decision"})
        knowledge_id = self.vector.add("a knowledge row", {"type": "knowledge"})

        self.assertIsNotNone(self.vector.get_by_id(decision_id, expected_type="decision"))
        self.assertIsNone(self.vector.get_by_id(knowledge_id, expected_type="decision"))

    def test_missing_and_malformed_ids_return_none(self):
        self.assertIsNone(self.vector.get_by_id(99999))
        self.assertIsNone(self.vector.get_by_id("not-an-int"))
        self.assertIsNone(self.vector.get_by_id(None))


if __name__ == "__main__":
    unittest.main()
