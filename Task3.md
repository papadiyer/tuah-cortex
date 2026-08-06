# TASK 3 — Build Jebat-Cortex Cognitive Runtime (Phase 3 / RC1)

You are **Lekiu** (Claude Code), Builder under Jebat/Hermes supervision.
Jebat is Architect & Memory Curator; Faisal is final approval authority.

This task implements Phase 3 of Task2 (RC1) for the `jebat-cortex` repo at
`~/dev/projects/the-magnificent-5/jebat-cortex`. The Phase 1 assessment
(`docs/COGNITIVE_RUNTIME_ASSESSMENT.md`) and Phase 2 design
(`docs/API_CONTRACTS.md`, `docs/EVENT_SCHEMA.md`, `docs/MEMORY_SCHEMA.md`,
`docs/MCP_SURFACE.md`) are the **source of truth**. Read them first. Do NOT
deviate from them; extend `core/*`, do not rewrite it.

## Hard constraints (non-negotiable)

1. `~/.hermes/state.db` is **read-only**. Reuse `core/ingest_hermes.py` ATTACH
   pattern. Never open it writable. Keep the before/after size assertion.
2. Token budgets: `user_char_limit=1375`, `memory_char_limit=2200` from
   `config/cortex_rules.json` are enforced server-side. The context digest must
   never exceed a supplied `token_budget`.
3. Embedding identity (`backend`, `model`, `dim`) is enforced at the preflight
   boundary: reuse `VectorStore.check_compatibility(raise_on_mismatch=True)`.
   Context build must **fail loud** (HTTP 409), never silent.
4. Bind API to `127.0.0.1` only (default port 8765). No public exposure.
5. No shell execution through the API. No commit/push/deploy. No secrets in logs
   (redact tokens/keys; reuse any existing redaction helper or add a small one).
6. Keep all 112 existing unit tests green. Add new tests (see below).
7. Smallest reliable deps. SQLite is already used — the durable queue MUST be
   SQLite-backed. Do NOT add Redis/Kafka/Temporal. For HTTP use FastAPI if
   available; otherwise a stdlib `http.server` based router is acceptable (keep
   it minimal). For MCP use a minimal stdio JSON-RPC server over `core/*` (do not
   pull a heavy MCP framework unless already present in the environment).
8. Use Python 3.9+ syntax (system Python is 3.9.6; do NOT use 3.10+ only syntax).

## Files to create

```
api/app.py            # FastAPI/ASGI app factory; mounts routers; localhost bind
api/models.py         # Pydantic/ dataclass request/response models
api/health.py         # GET /v1/health
api/context.py        # POST /v1/context/build  (uses core.context_builder)
api/events.py         # POST /v1/events/postflight (enqueue durable)
api/memory.py         # POST /v1/memory/search
api/projects.py       # GET /v1/projects/{id}/state
api/admin.py          # restricted admin (reindex/queue) — localhost token only
workers/queue.py      # SQLite durable queue (enqueue, claim, ack, dead-letter,
                      #   idempotent on request_id, observable depth)
workers/ingest_worker.py   # consume queue -> MemoryCurator.ingest_event -> done
mcp/server.py         # stdio MCP server exposing cortex.* tools (see MCP_SURFACE)
cli/cortex_cli.py     # CLI fallback: cortex health | build | search | serve
service/launchd/com.m5.jebat-cortex.plist   # macOS launchd, configurable paths
service/docker/docker-compose.yml           # optional secondary target
tests/test_api_health.py, test_api_context.py, test_api_events.py,
       test_queue.py, test_mcp_server.py     # new tests
```

## Required behaviour (from design docs)

- **Health**: returns status/graph_store/vector_store/embedding_identity/
  queue_depth/last_ingestion/version/uptime. `degraded` if a store is down.
- **Context build**: Tier 0 from config (`core/rules.py` + a small identity
  table), Tier 1 retrieved (knowledge vector + experience graph + decision/task
  tables scoped by project/entities), Tier 2 NOT auto-injected. Returns the full
  preflight payload incl. `provenance[]` and `warnings[]`. Respects budget.
- **Postflight**: persists event to SQLite queue **durably before 202**.
  Idempotent on `request_id`. Never spawns `&` jobs.
- **Memory search**: filters project/entity/date/memory_type/status/confidence/
  source/approved_only. Returns provenance.
- **MCP**: 7 tools (`cortex.search_memory`, `build_context`, `get_project_state`,
  `get_decision_history`, `get_related_experiences`, `record_outcome`, `health`).
  No destructive admin over MCP. Fail-loud on identity mismatch.
- **Queue worker**: survives restart (resets `processing`->`queued` on boot),
  bounded retry (exponential backoff, cap at 5 attempts), dead-letter beyond.

## Minimal additive core changes (non-breaking)

- `core/memory_curator.py`: add `ingest_event(event)` adapter over existing
  `ingest`; emit `provenance`/`status` in meta.
- `core/context_builder.py`: add `build_context(request)` returning the tiered
  preflight payload (reuse `retrieve`/`merge`/`apply_budget`).
- `core/vector_store.py` / `core/graph_store.py`: add `provenance`/`status` to
  `meta` (additive column; migrate gracefully).
- `run_cortex.sh`: keep as CLI fallback; no behaviour change.

## Verification (must pass before reporting done)

1. `python3 -m unittest discover -s tests` → all green (112 existing + new).
2. Start the service (`cli/cortex_cli.py serve` or `api/app.py`), then:
   - `GET /v1/health` returns `healthy` with correct embedding_identity.
   - `POST /v1/context/build` with a sample prompt returns a budgeted digest
     within `token_budget`, includes `provenance` and `warnings`.
   - `POST /v1/events/postflight` twice with same `request_id` → second is
     idempotent (no duplicate, `queued:false`).
   - The worker processes the queued event; a decision from the event becomes
     retrievable via `POST /v1/memory/search` with `status=approved`.
3. Run `bash run_cortex.sh --from-hermes` and assert `~/.hermes/state.db` size
   is unchanged (read-only proof).
4. Launch the MCP server (`python3.11 -m mcp.server`) and confirm `cortex.health`
   returns healthy (stdio smoke test).
5. Report actual latency numbers (do not fabricate).

## Do NOT

- Replace CrewAI / Tuah UI. Modify Hermes `state.db`. Merge Graphify into the
  conversational graph. Expose admin destructively over MCP. Bind publicly.
  Commit/push/deploy. Spawn `&` background jobs for ingestion.

## Report format

Structured completion: files created/modified, test results (counts), service
smoke-test evidence (curl/stdio output), state.db read-only proof, any deviation
from the design docs and why.
