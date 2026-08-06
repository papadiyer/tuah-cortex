# API Contracts — Jebat-Cortex Cognitive Runtime (RC1 / v0.4)

Design doc for Task2 Workstream 2. Defines the HTTP surface Tuah and other
clients use. All endpoints bind to `127.0.0.1:8765` by default (localhost-only).

## Conventions

- JSON bodies, `Content-Type: application/json`.
- Every request that mutates or builds context carries a `request_id`
  (UUID). The service is **idempotent** on `request_id` for postflight events
  (Workstream 5): a duplicate `request_id` returns the prior stored result
  without re-ingesting.
- `token_budget` is advisory for the context digest; the hard caps
  (`user_char_limit=1375`, `memory_char_limit=2200`) from `config/cortex_rules.json`
  are still enforced server-side. The response must never exceed the requested
  budget when one is supplied.
- Embedding identity is enforced at the preflight boundary: if the service's
  configured embedder does not match the knowledge store's stored identity,
  `/v1/context/build` fails with `409 embedding_identity_mismatch` (reuses
  `VectorStore.check_compatibility(raise_on_mismatch=True)`).
- Hermes `state.db` is accessed read-only; never opened writable by any endpoint.

## 1. Health — `GET /v1/health`

No body. Returns service liveness + store/queue state.

```json
{
  "status": "healthy",
  "version": "0.4.0",
  "graph_store": "ready",
  "vector_store": "ready",
  "embedding_identity": "deterministic",
  "queue_depth": 0,
  "last_ingestion": "2026-08-06T19:40:00+08:00",
  "uptime_seconds": 312
}
```

`status` is `healthy` only when both stores are open and the embedding identity
is internally consistent. `degraded` is returned (with `memory_status=degraded`)
if a store is unavailable — callers must not fabricate memory (Workstream 8).

## 2. Context Preflight — `POST /v1/context/build`

Mandatory before any M5 agent reasons (Task2 Core Decision). Builds the context
digest deterministically, then the agent reasons.

Request:

```json
{
  "request_id": "uuid",
  "actor": "faisal",
  "entrypoint": "tuah",
  "prompt": "user request",
  "project_hint": "optional",
  "session_id": "optional",
  "active_workspace": "optional",
  "token_budget": 2200,
  "permissions": { "read_memory": true, "write_memory": false, "execute": false }
}
```

Response:

```json
{
  "request_id": "uuid",
  "resolved_project": "jebat-cortex",
  "identity": { "backend": "deterministic", "model": "deterministic", "dim": 512 },
  "active_projects": [],
  "relevant_experiences": [],
  "relevant_knowledge": [],
  "relevant_decisions": [],
  "open_tasks": [],
  "relations": [],
  "recommended_agent": "jebat",
  "context_markdown": "budgeted digest",
  "provenance": [],
  "warnings": []
}
```

Behaviour:
- Tier 0 (always-loaded: Faisal authority, M5 roles, approval policy, Jebat
  identity, active project) is sourced from stable config/structured memory,
  **not** semantic retrieval.
- Tier 1 (decisions, recent experiences, unresolved tasks, people/agents,
  artefacts, project status, lessons) is retrieved for the current task.
- Tier 2 (full conversations, old docs, historical detail) is **not** injected
  automatically.
- `recommended_agent` is selected from the resolved context (default `jebat`).
- `warnings` carries non-fatal issues (e.g. `embedding_identity_mismatch`
  surfaced as a warning before a hard 409, or `graphify_unavailable`).
- Latency target: measured baseline, recorded in TEST_REPORT; not fabricated.

Errors: `400` invalid body/size; `409 embedding_identity_mismatch`; `503`
degraded (Cortex stores unavailable).

## 3. Postflight Event — `POST /v1/events/postflight`

Persists a completed-work event **durably before returning success** (Task2
Workstream 2: "do not rely on `some_command &`"). Enqueues to the SQLite queue;
the ingest worker curates it into memory asynchronously.

Request:

```json
{
  "request_id": "uuid",
  "session_id": "optional",
  "actor": "faisal",
  "agent": "jebat",
  "project": "optional",
  "prompt": "original request",
  "result_summary": "completed result",
  "status": "completed",
  "decisions": [],
  "lessons": [],
  "open_tasks": [],
  "artefacts": [],
  "provenance": [],
  "timestamp": "ISO-8601"
}
```

Response `202 Accepted`:

```json
{ "request_id": "uuid", "queued": true, "event_id": "evt_..." }
```

Idempotency: a second call with the same `request_id` returns the existing
`event_id` with `queued: false` (no duplicate ingestion). The event is written
to the durable store before `202` is returned; the worker consumes it later.

Hermes `state.db` is **never** written by this endpoint.

## 4. Memory Search — `POST /v1/memory/search`

Supports filters: `project`, `entity`, `date_range`, `memory_type`, `status`,
`confidence`, `source`, `approved_only`.

```json
{ "query": "optional free text", "filters": { "memory_type": "decision",
  "status": "approved", "project": "jebat-cortex", "limit": 20 } }
```

Returns matching memories with provenance (Workstream 4). Respects the same
embedding-identity enforcement as preflight.

## 5. Project State — `GET /v1/projects/{project_id}/state`

```json
{
  "project_id": "jebat-cortex",
  "objective": "...",
  "current_status": "...",
  "latest_approved_decisions": [],
  "unresolved_decisions": [],
  "open_tasks": [],
  "latest_artefacts": [],
  "related_agents": ["jebat", "lekiu"],
  "recent_experiences": []
}
```

## 6. Admin (restricted)

`POST /v1/admin/reindex`, `GET /v1/admin/queue` — restricted, not exposed via
MCP by default (Workstream 6, Workstream 10 #9). Require a local admin token;
never perform shell execution; never commit/push.

## Cross-cutting

- Input size limits (Workstream 10 #7): `prompt` ≤ 32k chars, `token_budget` ≤
  8192, bodies ≤ 256k bytes.
- Request timeout (Workstream 10 #8): preflight ≤ 5s p95 target.
- No secrets in logs (Workstream 10 #5); redact tokens/keys (reuse
  `lekiu_redact`).
- Structured logging fields: `request_id`, `session_id`, `actor`, `entrypoint`,
  `resolved_project`, `retrieval_count`, `context_tokens`, `duration_ms`,
  `memory_status`, `queue_status`, `error_code` (Workstream 11).
