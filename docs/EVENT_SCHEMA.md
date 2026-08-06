# Event Schema & Durable Queue — Jebat-Cortex (RC1 / v0.4)

Design doc for Task2 Workstream 5 (Durable Event Queue) and the postflight
half of Workstream 2. The queue is **SQLite-backed** (already a dependency) —
no Redis/Kafka/Temporal (Task2: smallest reliable implementation).

## 1. Postflight event (canonical record)

Emitted by Tuah after an agent finishes; persisted durably before the API
returns `202`.

```json
{
  "event_id": "evt_<uuid>",
  "request_id": "uuid",
  "session_id": "optional",
  "actor": "faisal",
  "agent": "jebat | lekiu | tuah | ...",
  "project": "optional-resolved",
  "prompt": "original request (redacted in logs)",
  "result_summary": "completed result",
  "status": "completed | failed",
  "decisions": [
    { "text": "...", "status": "proposed | approved | rejected", "project": "optional" }
  ],
  "lessons": [ { "text": "...", "project": "optional" } ],
  "open_tasks": [ { "title": "...", "status": "open", "project": "optional" } ],
  "artefacts": [ { "path_or_ref": "...", "kind": "file | doc | link" } ],
  "provenance": [ { "source_type": "agent_event", "source_id": "evt_...", "created_at": "ISO-8601" } ],
  "timestamp": "ISO-8601",
  "ingested": false,
  "ingested_at": null
}
```

## 2. Queue table (SQLite)

```sql
CREATE TABLE event_queue (
    event_id    TEXT PRIMARY KEY,
    request_id  TEXT NOT NULL UNIQUE,   -- idempotency key
    payload     TEXT NOT NULL,           -- canonical event JSON
    status      TEXT NOT NULL,           -- queued | processing | done | dead
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_attempt TEXT,                   -- ISO-8601, for bounded backoff
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_queue_status ON event_queue(status, next_attempt);
```

- **Idempotency**: `request_id` is `UNIQUE`. A duplicate postflight returns the
  existing row with `status` unchanged; no re-enqueue, no duplicate ingestion.
- **Survives restart**: the table is on disk; a crashed worker resumes from
  `status='queued' OR status='processing'` on boot (a `processing` row from a
  crash is reset to `queued`).
- **Bounded retry**: on failure, `attempts += 1`; if `attempts < MAX` (e.g. 5),
  `next_attempt = now + backoff(attempts)` (exponential, capped). Beyond max →
  `status='dead'` (dead-letter), still queryable, never silently dropped.
- **Observable**: `GET /v1/health` reports `queue_depth` = count of non-done
  rows; admin endpoint reports per-status counts.

## 3. Worker semantics (ingest_worker / consolidation_worker)

- `ingest_worker` polls `event_queue` for `queued` rows (oldest `next_attempt`),
  sets `processing`, calls `MemoryCurator.ingest_event(event)` (new thin adapter
  over existing `ingest`), then marks `done`. On exception → retry/backoff/dead.
- `consolidation_worker` (optional, later) merges related experiences, promotes
  repeated useful lessons (mirrors Graphify's `reflect`/`lessons` idea), and
  marks superseded memories `status='superseded'` rather than deleting.
- **Hermes `state.db` is read-only**: the worker reads it (if the event asks for
  a Hermes-derived ingest) via the existing `ingest_hermes.py` ATTACH pattern;
  it never opens `state.db` writable. The before/after size assertion stays.

## 4. Lifecycle (degraded mode)

If the queue or a store is unavailable:
- `/v1/events/postflight` returns `503` (does NOT background a `&` job).
- Tuah marks `memory_status=degraded`, logs the failure, continues only with
  current-session context, and does **not** fabricate memory (Workstream 8).

## 5. Files (Phase 3)

- `workers/queue.py` — SQLite queue (enqueue, claim, ack, dead-letter, idempotent).
- `workers/ingest_worker.py` — consume + curator ingest + mark done.
- `workers/consolidation_worker.py` — optional later.
- `core/memory_curator.py` — add `ingest_event(event)` adapter (non-breaking).
