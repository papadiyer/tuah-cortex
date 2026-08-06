# Cognitive Runtime Assessment — Jebat-Cortex → M5 Runtime

> Phase 1 deliverable for Task2 (RC1). Mandatory gate before any large-scale
> build. Assess current state, reuse, gaps, risks, migration, and non-goals.
> Do not begin Workstream 2+ implementation until this assessment is accepted.

## 1. Current architecture (verified this session)

Jebat-Cortex is a **standalone CLI pipeline**, not a service. One-shot flow:

```
conversation log (JSONL)
   -> MemoryCurator.ingest()          core/memory_curator.py
        - segment_message / classify / extract_relations / embed
        - splits into Knowledge (vector) + Experience (graph)
   -> VectorStore.add()               core/vector_store.py   (SQLite, cosine)
   -> GraphStore.add()                core/graph_store.py    (nodes/edges, ripgrep fallback)
   -> ContextBuilder.retrieve/merge/apply_budget   core/context_builder.py
        - hard budgets: user 1375, memory 2200
   -> Markdown/JSON digest -> injected into Jebat prompt
```

Ingestion sources:
- `core/ingest_hermes.py` — reads `~/.hermes/state.db` **read-only** (ATTACH),
  filters tool/empty rows, converts to the curator's JSONL. `run_cortex.sh
  --from-hermes` asserts `state.db` byte-size unchanged before/after.
- `run_cortex.sh` — sample + `--from-hermes` modes; resets generated stores
  before each run (no stale mixing).

Embedding identity (v0.3, hardened):
- `Embedder` hierarchy in `core/rules.py`: `DeterministicEmbedder` (default,
  no deps) + `SentenceTransformerEmbedder` (opt-in).
- Every vector row carries `embed_meta = {backend, model, dim}`.
- `VectorStore.add()` refuses incompatible identity; `query()` skips it;
  `ContextBuilder.retrieve()` calls `check_compatibility(raise_on_mismatch=True)`
  → production retrieval **fails loud**, never silent.

Graphify repo-brain layer (wired this session):
- `graphifyy` (double-y) installed in isolated `.venv-graphify/` (Python 3.11).
- `graphify extract "$PWD" --code-only --no-viz` builds `graphify-out/graph.json`
  locally, no LLM key.
- `graphify_context.sh` (delegate_coding_to_lekiu skill) wraps extract+query and
  is wired into `lekiu-task` → Lekiu gets structured repo context (ripgrep
  fallback if Graphify absent).

Tests: **112 unit tests**, all green. Remote: pushed to `origin/main` (private).

## 2. Reusable components (keep, extend)

| Module | Reuse as |
|---|---|
| `core/memory_curator.py` | Curator engine — wrap as a service-side ingest callable |
| `core/vector_store.py` | Knowledge store — already identity-safe; expose via API |
| `core/graph_store.py` | Experience graph — already has ripgrep fallback |
| `core/context_builder.py` | Context Builder — core of `/v1/context/build` |
| `core/rules.py` | Config + embedder factory + budget constants |
| `core/ingest_hermes.py` | Read-only Hermes access — reuse verbatim for postflight ingest |
| `run_cortex.sh` | CLI fallback / recovery entrypoint (Task2 requires CLI fallback) |
| Graphify wiring | Repo-brain layer for Lekiu/Codex — integrate via Workstream 7 |

## 3. Gaps (what RC1 must add)

- **No persistent service.** `run_cortex.sh` spawns a fresh process per run —
  explicitly disallowed as the primary production flow (Task2 Core Decision).
- **No HTTP API** (`/v1/health`, `/v1/context/build`, `/v1/events/postflight`,
  `/v1/memory/search`, `/v1/projects/{id}/state`).
- **No MCP adapter** (`cortex.search_memory`, `build_context`, `get_project_state`,
  `get_decision_history`, `get_related_experiences`, `record_outcome`, `health`).
- **No durable event queue** — postflight currently has no durable sink; must
  survive restart, idempotent on `request_id`, dead-letter, bounded retry.
- **No memory tiers** (Tier 0 always-loaded / Tier 1 task / Tier 2 on-demand).
- **No provenance / status schema** on memories (memory_id, type, status,
  source, confidence, project, entities).
- **No project state** model (objective, decisions, open tasks, artefacts).
- **No structured logging / observability** (request_id, latency, queue depth).
- **No service lifecycle** (launchd plist, Docker optional, health-check).
- **No security model doc** (threat model, redaction, localhost bind, input limits).
- **No Tuah integration contract doc.**

## 4. Risks

1. **Embedding identity at API edge.** The store enforces identity, but the
   service must reject a request whose configured embedder mismatches the store
   *before* building context — reuse `check_compatibility(raise_on_mismatch=True)`
   in the preflight handler.
2. **Budget enforcement under API.** `ContextBuilder.apply_budget` is proven in
   CLI; the API `token_budget` param must flow through unchanged. Add a test that
   the HTTP response respects the requested budget.
3. **Hermes `state.db` read-only.** Reuse `ingest_hermes.py` ATTACH pattern;
   keep the before/after size (and ideally hash) assertion in the postflight
   worker. Never open writable.
4. **Per-request process spawning.** Replace `run_cortex.sh`-per-call with a
   long-running service that holds open stores; ingest via queue, not `&`.
5. **Secret leakage.** `graphify_context.sh` already redacts; API logs must
   redact tokens/keys and never log full prompts by default.
6. **Localhost-only binding.** API must bind `127.0.0.1` by default; no public
   exposure (Task2 Security #4, Non-Goal).

## 5. Migration approach

**Extend, do not replace** (Task2 explicit). New top-level packages sit beside
`core/`:

```
jebat-cortex/
├── api/          # FastAPI app + routers (health, context, events, memory, projects)
├── mcp/          # server.py — stdio MCP adapter over the same core
├── workers/      # ingest_worker, consolidation_worker, queue (SQLite-backed)
├── service/      # launchd plist + docker compose
├── cli/          # cortex_cli.py — wraps api for debugging/recovery
├── core/         # UNCHANGED except thin adapters
├── docs/         # assessment, architecture, tuah, service, security, mcp, tests, release
└── tests/        # extend with API/integration tests
```

- Core modules are imported, not rewritten.
- The durable queue reuses SQLite (already a dependency) — no Redis/Kafka/Temporal
  (Task2 Workstream 5: smallest reliable implementation).
- MCP and HTTP both sit on top of the same `ContextBuilder` + stores.

## 6. Files proposed for creation / modification

**Create**
- `api/app.py`, `api/models.py`, `api/context.py`, `api/memory.py`,
  `api/events.py`, `api/health.py`, `api/projects.py`, `api/admin.py`
- `mcp/server.py`
- `workers/queue.py`, `workers/ingest_worker.py`, `workers/consolidation_worker.py`
- `cli/cortex_cli.py`
- `service/launchd/com.m5.jebat-cortex.plist`, `service/docker/docker-compose.yml`
- `docs/COGNITIVE_RUNTIME_ASSESSMENT.md` (this file), `docs/TUAH_CORTEX_INTEGRATION.md`,
  `docs/SERVICE_OPERATIONS.md`, `docs/SECURITY_MODEL.md`, `docs/MCP_INTEGRATION.md`,
  `docs/TEST_REPORT.md`, `docs/RELEASE_NOTES_v0.4.md`
- `tests/test_api_*.py`, `tests/test_mcp_*.py`, `tests/test_queue_*.py`

**Modify (minimal, adapter-only)**
- `core/context_builder.py` — add `build_context(request)` returning the full
  preflight payload (identity, tiers, provenance, warnings).
- `core/vector_store.py` / `core/graph_store.py` — optional `provenance` column
  for Workstream 4 (additive, non-breaking).
- `run_cortex.sh` — keep as CLI fallback; no behaviour change.

## 7. Explicit non-goals (per Task2)

- Do not replace CrewAI; do not build a new Tuah UI.
- Do not modify Hermes production DB.
- Do not merge Graphify + Cortex into one undifferentiated graph (separate
  provenance; Workstream 7).
- No Redis/Kafka/Temporal; no public network exposure; no autonomous commit/push.
- Do not ingest every raw conversation into prompts; do not treat all memories
  as equally trusted.

## 8. Recommended next step (Phase 2 Design gate)

Accept this assessment, then produce API contracts + event schema + memory
schema (provenance/status) + MCP surface as design docs, then delegate
Workstream 2 (service) build to Lekiu with Graphify context wired.
