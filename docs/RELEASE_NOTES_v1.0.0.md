# RELEASE NOTES — Jebat-Cortex v0.4.0 → v1.0.0 (GA)

**Status:** GA — released. All RC1 findings (2 High, 1 Medium, 1 Low) were
resolved and independently verified by Codex + Jebat; the runtime has since
proven correct behaviour superseding the default Hermes memory layer, and is
signed off for general availability by Faisal (final approval authority).

## What's in v1.0.0 (GA)

Transforms Jebat-Cortex from a standalone CLI pipeline into a **persistent local
cognitive runtime** for M5 (Task2).

| Capability | Module | Notes |
|---|---|---|
| HTTP API (localhost) | `api/` (FastAPI-style app + routers) | `/v1/health`, `/v1/context/build`, `/v1/events/postflight`, `/v1/memory/search`, `/v1/projects/{id}/state`, restricted `/v1/admin/*` |
| Context preflight | `api/context.py` + `core/context_builder.py` | Tier 0 (config) / Tier 1 (retrieved) / Tier 2 (on-demand, not auto-injected); budget-enforced |
| Durable event queue | `workers/queue.py` | SQLite-backed; survives restart; bounded retry; dead-letter; idempotent on `request_id` |
| Ingest worker | `workers/ingest_worker.py` | consumes queue → `MemoryCurator.ingest_event` → durable memory |
| MCP adapter | `mcp/server.py` | stdio; 7 `cortex.*` tools; no destructive admin over MCP |
| CLI fallback | `cli/cortex_cli.py` | `health | build | search | serve | worker | mcp` |
| Service lifecycle | `service/launchd/com.m5.jebat-cortex.plist` | macOS launchd; localhost only; configurable paths |
| Redaction | `core/redact.py` | recursive; applied at EVERY outward boundary (HTTP, MCP, CLI) |
| Decision approval gate | `api/service.py::approve_decision` | postflight decisions forced `proposed`; promotion only via authenticated admin (token + loopback) |
| Event read-back | `GET /v1/events/{event_id}` | returns lifecycle state (queued/done/dead) + redacted provenance |
| Graphify repo-brain | wired to `lekiu-task` | structured repo context for delegated coding (separate from conversational memory) |

## Verification evidence (RC1 freeze)

- **314/314 unit tests pass** (112 baseline + 202 new across API/MCP/queue/
  redaction/decision-auth/event-readback/approval-lookup).
- **Exact 1,001-decision repro**: approve id=1 → 200, id=500 → 200,
  id=1001 → 200, id=99999 → 404. Lookup is direct primary-key SQL via
  `VectorStore.get_by_id()` — no pagination ceiling.
- **Read-only Hermes**: `bash run_cortex.sh --from-hermes` leaves
  `~/.hermes/state.db` byte-identical (sha256 verified).
- **Redaction**: seeded `sk-...` / `Bearer` secrets return as `[REDACTED]` on
  HTTP + MCP + CLI wire bytes.
- **Auth gate**: unauthenticated `approved` decision coerced to `proposed`, not
  retrievable as approved; admin promotion requires token + loopback peer;
  remote peer cannot oracle the token; admin disabled → fail-closed.
- **Embedding identity**: `check_compatibility(raise_on_mismatch=True)` fails
  loud (409) at preflight, not silent.
- **Budgets**: `user_char_limit=1375` / `memory_char_limit=2200` enforced
  server-side; context digest stays within requested `token_budget`.
- **Docker**: explicitly EXCLUDED from RC1 (`service/docker/docker-compose.yml`
  tagged Non-RC1 — container binds 127.0.0.1 and is unreachable via host publish).

## Known follow-up (Low, not RC1 blocker)

- `ContextBuilder` does not yet track vector/graph ownership separately; suite
  emits `ResourceWarning` for unclosed SQLite connections in some paths.
  Tracked as a post-RC1 hardening task. Does not affect correctness or security.

## Upgrade / run

```bash
# service (run BOTH — serve does not auto-start the worker)
python3 -m cli.cortex_cli serve --port 8765 &
python3 -m cli.cortex_cli worker &
# MCP (Claude Code / Codex): point mcp.server at this repo root
python3 -m mcp.server
```

> RC1 does NOT auto-launch the worker from `serve`. The launchd plist must
> define two jobs (serve + worker), or use a wrapper. This is by design for
> clean shutdown; document it in SERVICE_OPERATIONS before GA.

## GA sign-off (v1.0.0)

- RC1 froze after all 4 findings resolved + independent Codex/Jebat verification.
- Post-RC1, the runtime demonstrated correct behaviour that **supersedes the
  default Hermes memory layer** for Jebat's working context (verified by Faisal
  through live use).
- v0.5 Expert-Axis Routing (Mixture-of-Experts at the memory/persona layer)
  shipped on top of RC1 and passed full verification: 376 tests green, zero
  ResourceWarning, embedding-identity guard intact, Hermes `state.db` read-only,
  config-only tunability proven, golden-set eval showing routed retrieval beats
  global.
- Faisal (final approval authority) signed off RC1 → GA.

## Commits
Phase 1 assessment + Phase 2 design → `9b25091`. Phase 3 runtime + Phase 3.1
blockers + Phase 3.2 follow-up → RC1. v0.5 Expert-Axis Routing → `18d7930`.
GA sign-off → this commit.
