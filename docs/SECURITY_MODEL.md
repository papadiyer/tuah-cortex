# Security Model — Jebat-Cortex (RC1 / v0.4)

Defines the security posture for the cognitive runtime. RC1 scope only.

## Threat model

Jebat-Cortex is a **local, single-user** cognitive runtime for Faisal/M5. The
trust boundary is the localhost loopback: anything reaching the API or MCP
server is assumed to be a local M5 agent or Faisal. Threats in scope:

1. **Memory poisoning** — an untrusted caller forges an "approved" decision or
   injects a secret into memory that later leaks back out.
2. **Secret exfiltration** — stored credentials surface through API/MCP/CLI
   responses.
3. **Unauthorized promotion** — a non-admin caller promotes a decision to
   `approved` (Tier 1 authority).
4. **Hermes data tampering** — Cortex must never modify the production Hermes
   `state.db`.
5. **Network exposure** — the API must not be reachable off-host.

Out of scope: multi-tenant auth, public deployment, supply-chain of dependencies
(assumed trusted local install), and Docker publishing (excluded from RC1).

## Controls (RC1)

### 1. Hermes `state.db` is read-only
- Ingest path opens `~/.hermes/state.db` **read-only** (ATTACH). Every run asserts
  the file size (and sha256) is unchanged before/after. Any write is rejected by
  the SQLite handle.
- No endpoint, worker, or MCP tool opens `state.db` writable.

### 2. Localhost-only binding
- HTTP API binds `127.0.0.1` by default (port 8765). Refuses `0.0.0.0`.
- MCP is stdio (no network). The admin approval route additionally checks the
  peer is loopback (`127.0.0.1` / `::1`); a remote peer is rejected **before**
  the token is evaluated, so the route cannot be used as a token oracle.

### 3. Redaction on every outward boundary
- `core/redact.py::redact_response` is applied recursively to all serialized
  responses: HTTP routers, MCP tool results, and CLI output. It is also a
  dispatcher-level backstop, so a future route that forgets to redact is still
  scrubbed on the wire.
- Patterns cover `sk-...`, `ghp_...`, `AKIA...`, `Bearer`, `postgres://user:pass@`,
  `api_key=...`, etc. Over-redaction is tested to be avoided (ordinary prose
  like "rotate the token next sprint" is left intact).

### 4. Decision approval is authenticated and gated
- Postflight (HTTP + MCP `record_outcome`) treats **every** submitted decision as
  `proposed`. A client-submitted `status='approved'` is **coerced to `proposed`**
  and the downgrade is logged (not silent).
- Promotion to `approved` happens ONLY via `POST /v1/admin/decision/{id}/approve`,
  which requires:
  - a valid `X-Cortex-Admin-Token` (env `CORTEX_ADMIN_TOKEN`), AND
  - a loopback peer.
- If `CORTEX_ADMIN_TOKEN` is unset, the admin route fails closed (403
  `admin_disabled`). The worker never promotes on its own.
- State machine: `proposed → approved`; `rejected`/`superseded` cannot be
  approved (409 `invalid_transition`).

### 5. No shell / no autonomous deploy
- No endpoint executes a shell. The durable queue is the ingestion mechanism; no
  `&` background jobs. Cortex never commits, pushes, or deploys.

### 6. Input limits & timeouts
- `prompt` ≤ 32k chars, `token_budget` ≤ 8192, bodies ≤ 256k bytes.
- Request timeouts applied at the service boundary.

### 7. Observability without leakage
- Structured logs carry `request_id`, `actor`, `entrypoint`, `resolved_project`,
  `retrieval_count`, `context_tokens`, `duration_ms`, `memory_status`,
  `queue_status`, `error_code` — never full prompts, never secrets (redaction
  applies to logs too).
- `GET /v1/events/{event_id}` returns lifecycle state + redacted provenance;
  it does NOT dump the original prompt payload.

## Secrets handling

- Secrets are **stored verbatim** in the vector store today (RC1 fixes the
  *read* boundary, not the *write* boundary). The stronger long-term control is
  curating secrets out at ingest time. Tracked as a post-RC1 follow-up.
- No secret is ever logged or returned unredacted.

## Failure posture

- **Fail closed**: missing admin token → 403; embedding-identity mismatch → 409;
  store unavailable → degraded (503), callers continue with session context only
  and must not fabricate memory.
- **No silent loss**: queue is durable; dead-lettered events remain queryable.
