# Codex Review Brief — Jebat-Cortex RC1

**From:** Faisal / Jebat (Hermes)
**Purpose:** Independent adversarial code review of Jebat-Cortex RC1 before release candidate freeze.
**Codex role:** Independent reviewer. NOT a replacement for our self-checks — we already ran full local verification (see `docs/PRE_CODEX_VERIFICATION.md`). Your job is to attack the assumptions we could not.

---

## Objective (RC1)

Turn Jebat-Cortex from a memory *pipeline* (v0.3) into a *cognitive runtime*:
- Expose the prefrontal memory pipeline over **HTTP API** (stdlib `http.server`, localhost-only), **MCP** (stdio JSON-RPC), and **CLI**.
- Add an **event queue** (SQLite-backed) for postflight/ingest events with worker durability.
- Add **secret redaction** (`core/redact.py`) for all logs/output.
- Keep **Hermes `~/.hermes/state.db` strictly read-only** (ingest path only).
- Enforce **approval policy**: no commit/push/merge/deploy/data-deletion without Faisal.

## Architecture Intent

```
prompt ─▶ ContextBuilder ─▶ tiered digest (Knowledge + Experience)
                                │
         EventQueue (SQLite) ───┘ (postflight/ingest events)
                                │
   API (127.0.0.1) ─── CortexService ─── MCP (stdio) ─── CLI
                                │
                   GraphStore + VectorStore (SQLite)
                                │
              Hermes state.db (READ-ONLY ingest via core/ingest_hermes.py)
```

All three surfaces (API/MCP/CLI) delegate to `CortexService` — same behaviour, no logic drift.

## Critical Invariants (attack these)

1. **localhost-only binding.** `api/app.py` must refuse any non-loopback host. Verify `server_bind` cannot be tricked (e.g. `0.0.0.0`, empty host, IPv6 mapping).
2. **Hermes state.db is NEVER written.** All ingest paths use `connect_readonly()`. Confirm no code path (queue worker, reindex, admin) opens it writable. This is the #1 safety contract.
3. **Approval policy is enforced in code, not docs.** `tier0.final_authority = Faisal`, no destructive op without explicit approval. Check admin routes require `CORTEX_ADMIN_TOKEN` and cannot be bypassed.
4. **Queue durability.** `data/event_queue.db` (SQLite) must survive process restart. Verify WAL/shm handling and that an unclean kill does not lose acknowledged events.
5. **Redaction actually fires.** `core/redact.py` (`lekiu_redact`) must scrub secrets from all log/API output. Test with a planted secret — does it leak?
6. **Embedding backend mismatch is loud, not silent.** `vector_store.check_compatibility()` must raise on a deterministic→SentenceTransformer flip (old vectors must not go unranked silently). This was a P1 fix in v0.3 — confirm it holds under RC1 refactors.
7. **Budget hard-caps.** `context_builder` must never exceed `memory_char_limit` (2200) / `user_char_limit` (1375), even on adversarial over-long input.

## Test Commands

```bash
cd /path/to/jebat-cortex
export PYTHONPATH="$PWD"
python3 -m unittest discover -s tests          # 259 tests, all green
python3 -m api.app --port 8765                 # API (background)
curl http://127.0.0.1:8765/v1/health
curl -X POST http://127.0.0.1:8765/v1/context/build -d '{"prompt":"test"}'
python3 -m mcp.server                          # MCP stdio; send tools/list
```

## Areas to Attack Adversarially

- **Event read-back gap:** `POST /v1/events/postflight` returns 202 + event_id, but `GET /v1/events/{id}` is 404. Is event data retrievable at all post-queue? Is the queue the only store? Data-loss risk?
- **Queue worker:** Is there a running worker that drains `event_queue`? Or does it only accumulate? What happens at 10k events?
- **MCP tool `cortex.record_outcome`:** append-only — can it be abused to poison memory (fake outcomes)?
- **Redaction bypass:** does redaction run on exceptions/stack traces? Multi-line secrets? Non-string payloads?
- **Path traversal:** API routes with `{project_id}` / file paths — any `../` escape?
- **Body size cap:** `max_body_bytes=256k` — does the handler enforce it before parse (DoS)?
- **SQLite concurrency:** API + worker + CLI all open the same DBs — any locked-write deadlock under load?
- **Version drift:** runtime reports 0.4.0; confirm code/config versions are consistent.

## What We Already Verified (do not re-litigate)

Tests green, API/MCP smoke OK, localhost binding, queue survives restart, Hermes read-only by construction, no secrets in diff. Focus your review on the **gaps and invariants above**, not the happy path.

---

## HANDOFF HIGHLIGHT — Event Read-Back Gap

**Observed behaviour (local verification, §4):**
- `POST /v1/events/postflight` returns **202 Accepted**, persists successfully to the SQLite queue (`data/event_queue.db`, `event_queue` table), and returns a valid `event_id`.
- **No `GET /v1/events/{id}` route exists** — attempts return 404 (`no route for GET /v1/events`). The event is written but cannot be queried/read-back via the API.

**Question for Codex — classify this as ONE of:**
1. **Acceptable RC1 scope** — write-only event sink is intentional; read-back deferred to a later phase.
2. **Observability gap** — events persist but operators cannot inspect them; needs a read route or admin query before RC1 freeze.
3. **API-contract defect** — the surface advertises event persistence but is incomplete; clients cannot retrieve what they created, implying silent data inaccessibility.

Determine which, with rationale. If (2) or (3), flag the missing route location (`api/events.py` → `app.py` route table) and any queue→store promotion logic that should back it.

## Deliverable Expected from Codex

- List of **High / Medium / Low** findings with file:line references.
- Specifically: is the **event read-back gap** a real data-loss bug or acceptable for RC1?
- Any invariant above that is **violated** with a concrete repro.
- Do NOT modify code. Report only. Faisal decides fixes.
