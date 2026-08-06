"""SQLite-backed durable event queue (EVENT_SCHEMA.md section 2).

SQLite is already a dependency, so the queue needs no broker: no Redis, no
Kafka, no Temporal. The properties the design asks for map onto plain SQL:

* **Durable** - the row is committed (and fsync'd by SQLite) before the API
  returns ``202``. A crash after the commit still leaves the event on disk.
* **Idempotent** - ``request_id`` is UNIQUE. A duplicate postflight returns the
  existing row instead of enqueueing a second copy.
* **Survives restart** - ``reset_stale_processing()`` on boot returns rows that
  a crashed worker left in ``processing`` back to ``queued``.
* **Bounded retry** - failures increment ``attempts`` and schedule an
  exponential, capped backoff; past ``max_attempts`` the row is dead-lettered
  rather than silently dropped.
* **Observable** - ``depth()`` and ``stats()`` back ``/v1/health`` and the admin
  endpoint.

Concurrency: a single ``BEGIN IMMEDIATE`` transaction guards claim(), so two
workers cannot take the same row.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_queue (
    event_id     TEXT PRIMARY KEY,
    request_id   TEXT NOT NULL UNIQUE,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_attempt TEXT,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON event_queue(status, next_attempt);
"""

STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_DEAD = "dead"

DEFAULT_MAX_ATTEMPTS = 5
# Exponential backoff, capped so a poison message cannot push retries a day out.
_BACKOFF_BASE_SECONDS = 2
_BACKOFF_CAP_SECONDS = 300


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Fields that do not change what gets ingested; ignored when comparing a
# duplicate request_id's payload against the stored one.
_VOLATILE_PAYLOAD_FIELDS = frozenset({"event_id", "timestamp", "ingested", "ingested_at"})


def _payload_differs(stored: Dict[str, Any], incoming: Dict[str, Any]) -> bool:
    """True when a re-sent request_id carries materially different content."""

    def _material(payload: Dict[str, Any]) -> str:
        trimmed = {
            k: v for k, v in (payload or {}).items() if k not in _VOLATILE_PAYLOAD_FIELDS
        }
        return json.dumps(trimmed, sort_keys=True, default=str)

    try:
        return _material(stored) != _material(incoming)
    except Exception:
        # Never let the comparison itself break an enqueue.
        return False


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def backoff_seconds(attempts: int, base: int = _BACKOFF_BASE_SECONDS, cap: int = _BACKOFF_CAP_SECONDS) -> int:
    """Exponential backoff for ``attempts`` failures, capped.

    attempts=1 -> 2s, 2 -> 4s, 3 -> 8s, 4 -> 16s, 5 -> 32s ... capped at 300s.
    """
    if attempts <= 0:
        return 0
    # Guard the shift: a corrupted attempts value must not overflow into a
    # multi-year delay.
    exponent = min(int(attempts), 20)
    return int(min(cap, base ** exponent if base > 1 else base * exponent))


class EventQueue:
    """Durable, idempotent, observable event queue on SQLite."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        rules: Optional[dict] = None,
    ):
        # Set before anything that can fail so close()/__del__ stay safe even
        # if construction aborts part-way.
        self._closed = False
        self.conn = None  # type: ignore[assignment]
        if db_path is None:
            from core.rules import load_rules, repo_path

            rules = rules or load_rules()
            configured = rules.get("paths", {}).get("queue_db", "data/event_queue.db")
            db_path = repo_path(configured)
        self.db_path = db_path
        self.max_attempts = int(max_attempts)
        if db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        # isolation_level=None -> autocommit; we manage transactions explicitly
        # so claim() can take an IMMEDIATE write lock.
        # check_same_thread=False: the API enqueues from request threads while
        # the worker claims from its own thread. Cross-process access is
        # already safe (SQLite file locking + BEGIN IMMEDIATE); cross-thread
        # use of this one connection is serialised by the caller's lock.
        self.conn = sqlite3.connect(
            db_path, timeout=30.0, isolation_level=None, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # WAL keeps a polling worker from blocking API writes. Not available on
        # :memory: databases, so failure here is not fatal.
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """Close the SQLite connection. Idempotent and safe to call twice.

        The connection object is kept (not set to None) so use-after-close
        raises sqlite3.ProgrammingError rather than AttributeError.

        Tolerates a partially-constructed instance (``__init__`` raised before
        the attributes existed), so error paths can always close defensively.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True
        conn = getattr(self, "conn", None)
        if conn is not None:
            conn.close()

    @property
    def closed(self) -> bool:
        """True once close() has run. Lets callers audit lifecycle state."""
        return self._closed

    def __del__(self) -> None:  # pragma: no cover - GC timing dependent
        """Best-effort backstop against a GC-time ResourceWarning."""
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "EventQueue":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- writes ------------------------------------------------------------
    def enqueue(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Persist an event durably. Idempotent on ``request_id``.

        Returns ``{event_id, queued, status, duplicate}``. ``queued`` is False
        when the request_id was already known - the caller must not treat that
        as an error, and must not re-ingest.
        """
        event = dict(event or {})
        request_id = str(event.get("request_id") or "").strip()
        if not request_id:
            raise ValueError("postflight event requires a request_id (idempotency key)")

        existing = self.by_request_id(request_id)
        if existing is not None:
            # Idempotency: return the prior result, do not re-ingest. But if the
            # caller reused a request_id with *different* content, the new
            # content is being discarded - say so rather than returning a
            # success-shaped response that hides the loss.
            result = {
                "event_id": existing["event_id"],
                "queued": False,
                "status": existing["status"],
                "duplicate": True,
            }
            if _payload_differs(existing.get("payload") or {}, event):
                result["payload_mismatch"] = True
                result["warning"] = (
                    "request_id %s was already used with different content; "
                    "the new payload was NOT stored" % request_id
                )
            return result

        event_id = str(event.get("event_id") or "").strip() or "evt_%s" % uuid.uuid4()
        event["event_id"] = event_id
        event.setdefault("ingested", False)
        event.setdefault("ingested_at", None)
        now = _iso(_utc_now())
        try:
            self.conn.execute(
                "INSERT INTO event_queue"
                " (event_id, request_id, payload, status, attempts, next_attempt,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    event_id,
                    request_id,
                    json.dumps(event, ensure_ascii=False, sort_keys=True),
                    STATUS_QUEUED,
                    now,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            # Lost a race against a concurrent identical request_id: the other
            # writer won, so report their row rather than failing the caller.
            duplicate = self.by_request_id(request_id)
            if duplicate is None:
                raise
            return {
                "event_id": duplicate["event_id"],
                "queued": False,
                "status": duplicate["status"],
                "duplicate": True,
            }
        return {"event_id": event_id, "queued": True, "status": STATUS_QUEUED, "duplicate": False}

    def claim(self) -> Optional[Dict[str, Any]]:
        """Atomically take the oldest due ``queued`` row and mark it processing.

        Returns the decoded row (with ``payload`` as a dict) or None when
        nothing is due. The IMMEDIATE transaction makes the select+update
        atomic, so two workers never claim the same event.
        """
        now = _iso(_utc_now())
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT * FROM event_queue"
                " WHERE status = ? AND (next_attempt IS NULL OR next_attempt <= ?)"
                " ORDER BY next_attempt, created_at"
                " LIMIT 1",
                (STATUS_QUEUED, now),
            ).fetchone()
            if row is None:
                self.conn.execute("COMMIT")
                return None
            self.conn.execute(
                "UPDATE event_queue SET status = ?, attempts = attempts + 1, updated_at = ?"
                " WHERE event_id = ?",
                (STATUS_PROCESSING, now, row["event_id"]),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        claimed = self._row_to_dict(row)
        claimed["status"] = STATUS_PROCESSING
        claimed["attempts"] = int(row["attempts"]) + 1
        return claimed

    def ack(self, event_id: str) -> bool:
        """Mark an event successfully processed."""
        now = _iso(_utc_now())
        cursor = self.conn.execute(
            "UPDATE event_queue SET status = ?, next_attempt = NULL, last_error = NULL,"
            " updated_at = ? WHERE event_id = ?",
            (STATUS_DONE, now, event_id),
        )
        return cursor.rowcount > 0

    def fail(self, event_id: str, error: str) -> Dict[str, Any]:
        """Record a processing failure: retry with backoff, or dead-letter.

        Returns ``{status, attempts, next_attempt}``. Beyond ``max_attempts``
        the row becomes ``dead`` - still queryable, never silently dropped.
        """
        row = self.conn.execute(
            "SELECT attempts FROM event_queue WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return {"status": "unknown", "attempts": 0, "next_attempt": None}

        attempts = int(row["attempts"])
        now = _utc_now()
        # Truncate the error: a huge traceback in the queue row helps nobody and
        # may carry payload text.
        message = (error or "")[:1000]

        if attempts >= self.max_attempts:
            self.conn.execute(
                "UPDATE event_queue SET status = ?, next_attempt = NULL, last_error = ?,"
                " updated_at = ? WHERE event_id = ?",
                (STATUS_DEAD, message, _iso(now), event_id),
            )
            return {"status": STATUS_DEAD, "attempts": attempts, "next_attempt": None}

        retry_at = _iso(now + timedelta(seconds=backoff_seconds(attempts)))
        self.conn.execute(
            "UPDATE event_queue SET status = ?, next_attempt = ?, last_error = ?,"
            " updated_at = ? WHERE event_id = ?",
            (STATUS_QUEUED, retry_at, message, _iso(now), event_id),
        )
        return {"status": STATUS_QUEUED, "attempts": attempts, "next_attempt": retry_at}

    def reset_stale_processing(self) -> int:
        """Return crashed-worker ``processing`` rows to ``queued`` (boot recovery).

        A row left in ``processing`` means the worker died mid-event; the event
        was never acked, so it must be retried. Returns the number requeued.

        Rows that have already burned ``max_attempts`` are dead-lettered
        instead. Without this a *poison event that hard-crashes the worker*
        (segfault/OOM/SIGKILL) never reaches ``fail()``, so the attempt ceiling
        is never evaluated and the event is retried forever - taking the worker
        down with it on every boot.
        """
        now = _iso(_utc_now())
        self.conn.execute(
            "UPDATE event_queue SET status = ?, next_attempt = NULL,"
            " last_error = COALESCE(last_error, ?), updated_at = ?"
            " WHERE status = ? AND attempts >= ?",
            (
                STATUS_DEAD,
                "worker died mid-event and exhausted max_attempts (no ack, no fail)",
                now,
                STATUS_PROCESSING,
                self.max_attempts,
            ),
        )
        cursor = self.conn.execute(
            "UPDATE event_queue SET status = ?, next_attempt = ?, updated_at = ?"
            " WHERE status = ?",
            (STATUS_QUEUED, now, now, STATUS_PROCESSING),
        )
        return int(cursor.rowcount or 0)

    # -- reads -------------------------------------------------------------
    def by_request_id(self, request_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM event_queue WHERE request_id = ?", (request_id,)
        ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def by_event_id(self, event_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM event_queue WHERE event_id = ?", (event_id,)
        ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def depth(self) -> int:
        """Count of rows still needing work (queued + processing).

        Dead-lettered rows are excluded: they are a separate alarm, reported by
        stats(), not backlog the worker will drain.
        """
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM event_queue WHERE status IN (?, ?)",
                (STATUS_QUEUED, STATUS_PROCESSING),
            ).fetchone()[0]
        )

    def stats(self) -> Dict[str, int]:
        """Per-status counts for the admin endpoint."""
        counts = {STATUS_QUEUED: 0, STATUS_PROCESSING: 0, STATUS_DONE: 0, STATUS_DEAD: 0}
        for row in self.conn.execute("SELECT status, COUNT(*) AS n FROM event_queue GROUP BY status"):
            counts[row["status"]] = int(row["n"])
        counts["total"] = sum(
            counts[k] for k in (STATUS_QUEUED, STATUS_PROCESSING, STATUS_DONE, STATUS_DEAD)
        )
        return counts

    def dead_letters(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM event_queue WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
            (STATUS_DEAD, int(limit)),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def last_done_at(self) -> Optional[str]:
        """Timestamp of the most recent successful ingestion, for /v1/health."""
        row = self.conn.execute(
            "SELECT updated_at FROM event_queue WHERE status = ?"
            " ORDER BY updated_at DESC LIMIT 1",
            (STATUS_DONE,),
        ).fetchone()
        return row["updated_at"] if row else None

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            payload = {}
        return {
            "event_id": row["event_id"],
            "request_id": row["request_id"],
            "payload": payload,
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "next_attempt": row["next_attempt"],
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
