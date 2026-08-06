# TASK 4 — RC1 Blockers: Redaction + Auth Gate + Read-back (Phase 3.1)

You are **Lekiu** (Claude Code), Builder under Jebat/Hermes supervision.
This task fixes RC1 blocker findings from independent verification of Phase 3.
Source of truth: `docs/API_CONTRACTS.md`, `docs/EVENT_SCHEMA.md`,
`docs/MEMORY_SCHEMA.md`, `docs/SECURITY_MODEL.md` (if present), and the existing
`core/redact.py` (already created in Phase 3 — reuse it). Read them first.

## Blocker verdict (from Faisal / verification)
RC1 must NOT freeze until both HIGH findings + the event read-back gap are fixed.
Docker is EXCLUDED from RC1 scope (leave `service/docker/docker-compose.yml` but
mark it non-RC1 in a comment; do not make it a blocker).

## P1-HIGH-1 — Redaction bypasses on outward responses

Current bug: `core/redact.py` (lekiu_redact) is only applied to some logs/errors.
Successful HTTP responses, MCP tool outputs, and CLI output are serialized
WITHOUT redaction, so secrets stored in memory leak back through
`/v1/memory/search`, `/v1/context/build`, and MCP `cortex.*` outputs.

**Fix:**
- Apply recursive redaction to EVERY outward response boundary: all `api/*`
  routers (health, context, events, memory, projects, admin), `mcp/server.py`
  (every tool result), and `cli/cortex_cli.py` output.
- Reuse `core/redact.py::lekiu_redact` (or extend it if a pattern is missing).
  Ensure it is recursive over dict/list/str.
- Redact at the serialization boundary (wrap the response payload) so no caller
  forgets to call it.

**Reproducer test (must pass):** seed a memory containing a fake secret
`sk-1234567890abcdef` (and an `Authorization: Bearer xyz` style token). Call
`GET/POST /v1/memory/search`, `POST /v1/context/build`, and invoke MCP
`cortex.search_memory` / `cortex.build_context` for that memory. Assert the
serialized response body contains `[REDACTED]` (or the redaction token) and
NEVER the verbatim `sk-1234567890abcdef`. This is a regression test — add it.

## P1-HIGH-2 — Unauthenticated clients can mint approved decisions

Current bug: `POST /v1/events/postflight` accepts `decisions[].status='approved'`
with no authentication; the worker copies that status straight into durable
memory; `cortex.record_outcome` (MCP) uses the same path. Any caller (including
MCP) can poison Tier 1 authority — a reproducer returned "Deploy to production
immediately" as `approved`.

**Fix (policy from Faisal):**
- Postflight (HTTP + MCP) MUST treat every submitted decision as `proposed` by
  default. If a client submits `status='approved'`, COERCE it to `proposed`
  (do not store `approved` from an unauthenticated postflight).
- Promotion to `approved` requires an AUTHENTICATED admin transition, localhost
  only. Add `POST /v1/admin/decision/{decision_id}/approve` that:
  - requires a valid admin token (`CORTEX_ADMIN_TOKEN` env var; header
    `X-Cortex-Admin-Token`);
  - is bound to localhost (reject if not 127.0.0.1);
  - transitions the decision `proposed -> approved` and records who/when.
- The worker only ever stores what postflight gave it (`proposed`); it never
  promotes. `approved` exists ONLY after the admin endpoint is called.
- `cortex.get_decision_history` / `memory/search` with `status=approved` must
  NOT return decisions that were submitted as `approved` via postflight (they are
  `proposed`).

**Reproducer test (must pass):**
1. `POST /v1/events/postflight` with `decisions:[{text:"Deploy to production
   immediately", status:"approved"}]` from an UNAUTHENTICATED caller → 202.
2. Worker drains it. `POST /v1/memory/search` / decision history with
   `status=approved` → the "Deploy..." decision is NOT returned as approved
   (it is `proposed`). Assert this.
3. Call `POST /v1/admin/decision/{id}/approve` with the correct admin token
   (localhost) → 200; now decision history WITH `status=approved` returns it.
4. Same admin call WITHOUT the token → 401; decision stays `proposed`.

## P2-MEDIUM — Event read-back route (observability gap)

Add `GET /v1/events/{event_id}` returning the event's lifecycle state:
`queued | processing | done | dead`, plus redacted provenance. Protect payload
details (do not dump the full original prompt/sensitive fields; return status +
timestamps + provenance only, redacted). The queue already has `by_event_id()`
(Phase 3) — reuse it.

**Reproducer test:** post an event, GET its `{event_id}` → returns `queued` (or
later `done` after worker runs); a non-existent id → 404.

## Constraints (same as Task3)
- `~/.hermes/state.db` read-only (never opened writable).
- Budgets 1375/2200 enforced; embedding identity fail-loud (409).
- Localhost only; no public bind; no shell exec; no commit/push.
- Keep all 259 existing tests green; ADD regression tests for both P1 + read-back.
- Python 3.9+ syntax.

## Files to touch (minimal, additive)
- `api/events.py` — coerce decision status to `proposed`; add admin approve route.
- `api/memory.py`, `api/context.py`, `api/projects.py`, `api/health.py`,
  `api/admin.py` — wrap responses with recursive redaction.
- `mcp/server.py` — redact every tool result.
- `cli/cortex_cli.py` — redact output.
- `core/redact.py` — extend patterns if a secret shape is missing.
- `tests/test_redact_boundary.py` (new), `tests/test_decision_auth.py` (new),
  `tests/test_event_readback.py` (new).
- `service/docker/docker-compose.yml` — add comment: "Non-RC1 / excluded from
  RC1 scope."

## Verification (must pass before reporting done)
1. `python3 -m unittest discover -s tests` → all green (259 + new).
2. Redaction reproducer: secret `sk-...` is `[REDACTED]` on HTTP + MCP + CLI.
3. Auth reproducer: unauthenticated `approved` decision lands as `proposed`;
   admin approve (with token, localhost) promotes; without token → 401.
4. Read-back: `GET /v1/events/{id}` returns lifecycle state; 404 for unknown.
5. `bash run_cortex.sh --from-hermes` → state.db size UNCHANGED (read-only proof).
6. Live smoke (localhost): health healthy; context build redacts a seeded
   secret; postflight approved-coerced; admin approve works; event read-back
   returns state.

## Report format
Files changed; test counts; the 3 reproducer results (actual output); state.db
read-only proof; any deviation and why. Do NOT claim done on green tests alone —
show the reproducer evidence.
