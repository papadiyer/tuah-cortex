"""Ingest worker: drain the durable queue into curated memory.

EVENT_SCHEMA.md section 3. The worker is a plain polling loop, not a background
``&`` job spawned per request: the API only ever writes to the queue, and this
process is the single consumer that turns events into memory.

Boot recovery: rows left in ``processing`` by a crashed worker are reset to
``queued`` before the loop starts, so no event is stranded.

Hermes ``state.db`` is never opened writable here; this worker only touches the
Cortex-owned vector/graph/queue databases.

Run as::

    python3 -m workers.ingest_worker            # follow mode (poll forever)
    python3 -m workers.ingest_worker --once     # drain what is due, then exit
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from typing import Any, Dict, Optional

from core.memory_curator import Curator
from core.redact import lekiu_redact
from core.rules import load_rules
from core.vector_store import VectorStore
from core.graph_store import GraphStore
from workers.queue import EventQueue

LOGGER = logging.getLogger("cortex.ingest_worker")


class IngestWorker:
    """Consumes event_queue rows and curates them into memory."""

    def __init__(
        self,
        queue: Optional[EventQueue] = None,
        curator: Optional[Curator] = None,
        rules: Optional[dict] = None,
    ):
        self.rules = rules or load_rules()
        service = self.rules.get("service", {})
        self.queue = queue if queue is not None else EventQueue(
            max_attempts=int(service.get("queue_max_attempts", 5)), rules=self.rules
        )
        self._owns_queue = queue is None
        self._owns_curator = curator is None
        try:
            self.curator = curator if curator is not None else Curator(rules=self.rules)
        except Exception:
            # The queue may already be open; do not strand it.
            if self._owns_queue:
                try:
                    self.queue.close()
                except Exception:
                    pass
            raise
        self.poll_seconds = float(service.get("worker_poll_seconds", 1.0))
        self._stop = False
        self._closed = False

    def close(self) -> None:
        """Close the curator/queue this worker created. Idempotent.

        Injected handles belong to the caller (CortexService shares its own
        stores and queue with the worker) and must not be closed here.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True
        for owned, obj in (
            (getattr(self, "_owns_curator", False), getattr(self, "curator", None)),
            (getattr(self, "_owns_queue", False), getattr(self, "queue", None)),
        ):
            if not owned or obj is None:
                continue
            try:
                obj.close()
            except Exception:
                pass

    def __del__(self) -> None:  # pragma: no cover - GC timing dependent
        """Best-effort backstop against a GC-time ResourceWarning."""
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "IngestWorker":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def stop(self, *_: Any) -> None:
        """Ask the follow loop to finish the current event and exit."""
        self._stop = True

    # -- single event ------------------------------------------------------
    def process_one(self) -> Optional[Dict[str, Any]]:
        """Claim and process one due event. Returns a result summary or None."""
        claimed = self.queue.claim()
        if claimed is None:
            return None

        event_id = claimed["event_id"]
        try:
            report = self.curator.ingest_event(claimed["payload"])
        except Exception as exc:
            # Redact before logging: the event payload carries prompt text.
            outcome = self.queue.fail(event_id, "%s: %s" % (type(exc).__name__, exc))
            LOGGER.warning(
                "event %s failed (attempt %d) -> %s: %s",
                event_id,
                claimed["attempts"],
                outcome["status"],
                lekiu_redact(str(exc))[:300],
            )
            return {
                "event_id": event_id,
                "ok": False,
                "status": outcome["status"],
                "attempts": claimed["attempts"],
                "error": lekiu_redact(str(exc))[:300],
            }

        self.queue.ack(event_id)
        LOGGER.info("event %s ingested: %s", event_id, report.get("counts"))
        if report.get("skipped_count"):
            # Surface duplicate-suppressed items; a drop must never be invisible.
            LOGGER.warning(
                "event %s: %d item(s) skipped as duplicates: %s",
                event_id,
                report["skipped_count"],
                lekiu_redact(report.get("skipped")),
            )
        return {"event_id": event_id, "ok": True, "status": "done", "report": report}

    # -- loops -------------------------------------------------------------
    def drain(self, max_events: int = 1000) -> Dict[str, Any]:
        """Process every currently-due event, then return. Used by --once/tests."""
        processed = 0
        failed = 0
        for _ in range(max(0, int(max_events))):
            result = self.process_one()
            if result is None:
                break
            if result["ok"]:
                processed += 1
            else:
                failed += 1
        return {"processed": processed, "failed": failed, "depth": self.queue.depth()}

    def run_forever(self) -> None:
        """Poll until stopped. Resets crashed rows before starting."""
        reset = self.queue.reset_stale_processing()
        if reset:
            LOGGER.info("boot recovery: reset %d stale processing row(s) to queued", reset)
        LOGGER.info("ingest worker started (poll=%.1fs, db=%s)", self.poll_seconds, self.queue.db_path)
        while not self._stop:
            try:
                if self.process_one() is None:
                    time.sleep(self.poll_seconds)
            except Exception as exc:  # pragma: no cover - loop must not die
                LOGGER.error("worker loop error: %s", lekiu_redact(str(exc))[:300])
                time.sleep(self.poll_seconds)
        LOGGER.info("ingest worker stopped")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Jebat-Cortex ingest worker")
    parser.add_argument("--once", action="store_true", help="drain due events then exit")
    parser.add_argument("--queue-db", default=None, help="override queue db path")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    rules = load_rules()
    queue = EventQueue(args.queue_db, rules=rules) if args.queue_db else None
    worker = IngestWorker(queue=queue, rules=rules)
    # Reset any stale rows even in --once mode; a crash must never strand work.
    worker.queue.reset_stale_processing()

    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)

    try:
        if args.once:
            summary = worker.drain()
            print(json.dumps(summary, indent=2))
        else:
            worker.run_forever()
    finally:
        worker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
