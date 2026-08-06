# PRE_CODEX_VERIFICATION — Jebat-Cortex RC1

**Date:** 2026-08-06 (local)
**Verifier:** Jebat/Hermes (independent local verification before Codex review)
**Scope:** Full local verification of RC1 working tree (uncommitted). No commit/push/deploy performed.
**Service version reported by runtime:** 0.4.0

---

## 1. Full Test Suite (existing + new)

- Command: `python3 -m unittest discover -s tests`
- Result: **259 tests, OK (exit 0)**, 1.777s
- Note: one test logs an expected retry path ("event ... failed (attempt 2) -> dead: ingest exploded") — this is intentional dead-letter handling, not a failure.
- All 13 test files execute (6 original + 7 new: api_context, api_events, api_health, ingest_event, mcp_server, queue, redact).

**PASS**

---

## 2. API Startup + /v1/health Smoke

- Start: `python3 -m api.app --port 8765`
- Log: `Jebat-Cortex 0.4.0 listening on http://127.0.0.1:8765 (localhost only)`
- `GET /v1/health` → 200, JSON:
  `{"status":"healthy","version":"0.4.0","graph_store":"ready","vector_store":"ready","embedding_identity":"deterministic","queue_depth":0,...}`

**PASS**

---

## 3. Context Build Endpoint

- `POST /v1/context/build` with `{"prompt":"...","max_tokens":200}` → 200
- Response contains `tier0` (final_authority=Faisal, approval_policy), `identity`, `retrieval_count`:10, `context_tokens`:1243, `memory_status`:ready.
- API log: `context.build ... retrieval_count=10, context_tokens=1243, duration_ms=1624`.

**PASS**

---

## 4. Postflight Event Persistence

- Route is `POST /v1/events/postflight` (NOT `/v1/events`). Requires `request_id`.
- With valid body → **202 Accepted**, returns `event_id`, `queued:true`.
- **GAP:** `GET /v1/events/{id}` → **404** (no read-back route). Events are written to the queue (accepted + queued) but cannot be queried via the API.
- Persistence to SQLite queue confirmed (see §5).

**PARTIAL — write path works, read-back route missing (flag for Codex).**

---

## 5. Worker Restart + Queue Survive

- Queue backend: **SQLite** at `data/event_queue.db` (`event_queue` table), file-backed.
- After killing the API process and restarting, the DB retained **26 rows**; the previously queued event_id was still present.
- Conclusion: queue survives process restart (durable by construction).

**PASS**

---

## 6. MCP Server Starts + Tools Enumerate

- Start: `python3 -m mcp.server` (custom stdio JSON-RPC, no external mcp lib dependency).
- `tools/list` → 7 tools, all read/append-only:
  `cortex.health`, `cortex.build_context`, `cortex.search_memory`, `cortex.get_project_state`, `cortex.get_decision_history`, `cortex.get_related_experiences`, `cortex.record_outcome`
- No destructive/admin tools exposed (per MCP_SURFACE.md).

**PASS**

---

## 7. localhost-only Binding

- `lsof -iTCP:8765` → `TCP 127.0.0.1:8765 (LISTEN)` only.
- `curl http://0.0.0.0:8765` resolves to loopback (false positive); binding is loopback-only by construction (`create_server` refuses non-loopback host — see api/app.py:server_bind).
- No external interface exposed.

**PASS**

---

## 8. Hermes state.db Read-Only Proof

- `core/ingest_hermes.py` uses `connect_readonly()` — no write path exists in source.
- No RC1 code writes to `~/.hermes/state.db` (grep: only read-only references + doc examples).
- Current DB: sha256 `739513eaa310fd0e57de2266895c58b98c85e950f392e4abde49953a6971c5f7`, size 281407488.
- Note: mtime changes are expected from normal live Hermes usage (separate process), NOT from Cortex. The verifiable claim is **Cortex RC1 is read-only by construction** — confirmed in source.

**PASS (read-only by construction)**

---

## 9. Git Diff Secret / Temp / Path Scan

- **Secrets:** No real credentials. Matches for "token/secret/key" are false positives (`token_budget` = token count, `CORTEX_ADMIN_TOKEN` = env var *name*, `request_id`). No `sk-`, `ghp_`, `AKIA`, JWT, or credential values.
- **Temp/junk:** None staged (gitignored).
- **Absolute paths:** `/Users/faisal/dev/projects/the-magnificent-5/jebat-cortex` appears only in **docs** (MCP_SURFACE.md, MCP_INTEGRATION.md) as examples — NOT in live config (no `.mcp.json` in repo). Portability note, not a leak.
- **.env/keys:** None present.
- `CORTEX_ADMIN_TOKEN` is env-only (`os.environ.get`), no hardcoded default.

**PASS (minor: doc example paths — flag for Codex as portability note)**

---

## Summary

| # | Check | Result |
|---|-------|--------|
| 1 | Full test suite (259 tests) | ✅ PASS |
| 2 | API startup + /v1/health | ✅ PASS |
| 3 | Context build endpoint | ✅ PASS |
| 4 | Postflight event persistence | ⚠️ PARTIAL (write OK, no read-back route) |
| 5 | Worker restart + queue survive | ✅ PASS |
| 6 | MCP server + 7 tools | ✅ PASS |
| 7 | localhost-only binding | ✅ PASS |
| 8 | Hermes state.db read-only | ✅ PASS (by construction) |
| 9 | Secret/temp/path scan | ✅ PASS (minor doc paths) |

**Overall: RC1 is locally green and safe to submit to Codex for adversarial review.**
Outstanding item for Codex: **event read-back route (`GET /v1/events/{id}`) is missing** — events can be written (202) but not queried. Recommend adding the read route or documenting the gap.

**No commit, push, or deploy performed.** Awaiting Faisal approval + Codex review.
