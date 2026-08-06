# Runtime Notes — Phase 3 (RC1 / v0.4)

Implementation notes for the cognitive runtime. Records the environment facts
and the deviations from the Phase 2 design docs, with the reason for each.

## 1. Environment as found (measured, 2026-08-06)

| Fact | Value |
|---|---|
| `python3` on PATH | Homebrew **3.14.6** (`/opt/homebrew/bin/python3`) |
| Ambient `PYTHONPATH` | `~/.hermes/hermes-agent` + that venv's **python3.11** `site-packages` |
| FastAPI / uvicorn / pydantic / `mcp` | **not importable** in a clean interpreter |
| ripgrep | available (optional fallback) |

### The PYTHONPATH trap

The shell exports a `PYTHONPATH` pointing at Hermes' **Python 3.11** venv, while
`python3` is **3.14**. That makes dependency probing actively misleading:

```
python3 -c "import importlib.util as u; print(bool(u.find_spec('fastapi')))"   # True
python3 -c "import fastapi"
# ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'
```

`find_spec` finds the 3.11 package directory, but the compiled extension
(`pydantic_core`) is built for 3.11 ABI and cannot load under 3.14. Verify
dependencies with `python3 -E -c "import fastapi"` (`-E` ignores `PYTHONPATH`),
never with `find_spec` alone.

**Consequence:** FastAPI is not usable. The task's sanctioned fallbacks are used:

- HTTP: stdlib `http.server` + a small regex router (`api/app.py`).
- Models: dataclass validators (`api/models.py`) instead of pydantic.
- MCP: minimal stdio JSON-RPC (`mcp/server.py`) instead of the `mcp` SDK.

Net third-party dependencies added: **zero**. If FastAPI is installed later,
`api/service.py` is already framework-agnostic — only `api/app.py` changes.

### Python version

The task brief stated system Python is 3.9.6; it is actually 3.14.6. Code is
written to the **3.9** feature set anyway (no `match`, no `X | Y` annotations,
`from __future__ import annotations` throughout) so it runs on either.

## 2. Two defects found by the smoke test, and fixed

### 2.1 A 35-second startup stall (`api/app.py`)

`HTTPServer.server_bind` calls `socket.getfqdn(host)` to set `server_name`. On
this machine that reverse lookup blocks for **35.07 s** (measured), on every
start and every test that opens a socket. `_LocalhostServer.server_bind` now
skips it and hard-codes `localhost`, which is correct because the server refuses
to bind anything but loopback. Test time for `test_api_health.py`: 35.3 s →
0.20 s.

### 2.2 Cross-thread SQLite use (`core/*`, `api/app.py`)

`ThreadingHTTPServer` serves each connection on a new thread, but SQLite
connections are pinned to their creating thread. The first live `GET /v1/health`
returned `degraded` with *"SQLite objects created in a thread can only be used in
that same thread"* — the in-process tests never caught it because they are
single-threaded.

Fix: `check_same_thread=False` on the three stores, plus a `threading.Lock`
around handler dispatch in `CortexApp`, so the shared connections are never used
concurrently. Requests are short local reads, so serialising them costs little
next to the correctness guarantee.

## 2b. Two contract violations found by adversarial review, and fixed

Both were in code written for this task, both were caught by an independent
review pass after the first green test run, and both now have regression tests.

### 2b.1 The digest exceeded the requested `token_budget` (`core/context_builder.py`)

`_digest_limit()` capped the digest at `budget + user_char_limit + 1200`, so a
small budget was blown by more than 1000 characters:

```
token_budget=10   requested_chars=40    context_chars=1388   EXCEEDS by 1348
token_budget=100  requested_chars=400   context_chars=1740   EXCEEDS by 1340
```

API_CONTRACTS.md says the response *must never* exceed the requested budget.
Fixed: when `token_budget` is supplied the cap is exactly `token_budget * 4`
characters, Tier 0 included. Tier 0 renders first, so a tiny budget keeps the
authority block and sheds retrieved memory — losing a decision is recoverable,
losing "Faisal is the final authority" is not. Truncation now also appends
`context_truncated_to_token_budget` to `warnings`, so a clipped digest never
looks complete. Verified after the fix: budget 10/50/100/200/400 → digest
always within cap.

### 2b.2 Silent memory loss on identical text across events (`core/memory_curator.py`)

`VectorStore.add` fingerprints on `"source::text"` to stop re-ingesting the same
log line. `ingest_event()` set `source` to the constant `"agent_event"`, so the
*same decision sentence recorded by a second event — even for a different
project — was swallowed by `INSERT OR IGNORE` while the worker acked the event
as fully processed*:

```
event1 (project-alpha, approved) decisions stored: 1
event2 (project-beta,  proposed) decisions stored: 0   <-- lost, reported OK
project-beta search -> []
```

That is exactly the silent memory loss this codebase forbids. Fixed two ways:

1. `source` is now scoped per event (`agent_event:<event_id>`), so duplicate
   suppression still works *within* an event but two events keep their own rows
   with their own project and status.
2. Any item the store does refuse is returned in `skipped[]` / `skipped_count`
   and logged by the worker at WARNING. A drop is now always visible.

## 2c. Second review round — seven more defects, all fixed

A second adversarial pass found seven further issues. Each was independently
reproduced before being fixed, and each now has a regression test.

| # | Severity | Defect | Fix |
|---|---|---|---|
| 1 | HIGH | **Tier 1 was not project-scoped.** `retrieve()` ranked knowledge/experience across *all* projects; only decisions/tasks filtered. Another client's memory could be injected into a `jebat-cortex` prompt. | `vector.query()` / `graph.query()` now take `project=` and return that project's rows **plus project-less global rows**; `build_context` passes the resolved project. |
| 2 | HIGH | **Silent retrieval failure.** A store error in the approved-decisions lookup was swallowed by a bare `except`, returning HTTP 200 with `decisions: []` and no warning — indistinguishable from "no decisions exist". It also swallowed the identity `ValueError` the 409 path depends on. | Failures append `<type>_lookup_failed:<Error>` to `warnings`; `ValueError` is re-raised so 409 still fires. |
| 3 | HIGH | **Poison event never dead-lettered.** `claim()` bumps `attempts` but only `fail()` checks `max_attempts`. A worker killed mid-event (OOM/SIGKILL) never calls `fail()`, so boot recovery requeued it forever — the event took the worker down on every restart. | `reset_stale_processing()` dead-letters rows already at `max_attempts`. |
| 4 | MED | **Leaked connection on partial store failure.** If vector opened and graph then failed, both attrs were set to `None`, orphaning the live vector connection — during a disk-full/locked-DB boot, the worst time to leak handles. | The store that did open is closed first (only if we opened it; injected stores belong to the caller). |
| 5 | MED | **`drain_queue()` ignored degraded state** and passed `None` stores to `Curator`, which then opened **the real production databases** and ingested into them while the service believed it was degraded. | Refuses to run unless `_stores_ready()`. |
| 6 | MED | **Reused `request_id` with different content** returned a success-shaped `202` and silently discarded the new payload. | Payloads are compared (ignoring `event_id`/`timestamp`); a divergence sets `payload_mismatch` + a warning and is logged. The first payload still wins — that is the idempotency contract. |
| 8 | LOW | **413 desynced HTTP/1.1 keep-alive.** The unread body was parsed as the next request line (spurious `414`, then a broken pipe). A naive close also caused a TCP RST that destroyed the 413 before the client could read it. | Bounded drain (≤4 MB) so the response is delivered, then `Connection: close`. |
| 9 | LOW | **LIKE metacharacters unescaped** in the `entity` filter: `entity='%'` returned the entire store. Not injection (values were bound), but a filter-bypass. | `%`/`_`/`\` escaped with `ESCAPE '\'` in both stores. |

**One reported finding was disproven.** The review claimed the per-event
fingerprint (fix 2b.2) would make *retries* create duplicate rows. It does not:
`event_id` is stable across retries of the same event, so the fingerprint is
identical and the duplicate is still suppressed — verified directly. The
non-transactional nature of `ingest_event` is real but benign for this reason;
worth revisiting only if a future change makes `event_id` per-attempt.

## 3. Deviations from the design docs

| Design doc | Deviation | Why |
|---|---|---|
| API_CONTRACTS "FastAPI" implied | stdlib `http.server` | FastAPI not installed; task allows this fallback |
| API_CONTRACTS pydantic models | dataclass validators | pydantic unusable (broken ABI) |
| MCP_SURFACE `mcp` SDK | stdlib JSON-RPC | SDK not installed; design allows "minimal stdio JSON-RPC" |
| API_CONTRACTS `lekiu_redact` "reuse" | newly written | no redaction helper existed in the repo |
| `/v1/health` degraded → 503 | returns **200** with `status:"degraded"` | a health endpoint must stay readable when degraded; endpoints that cannot serve correctly still return 503 |

## 4. Measured latency (real store: 20,451 knowledge rows, 3,392 edges)

| Operation | Latency |
|---|---|
| `GET /v1/health` | ~27–67 ms |
| `POST /v1/context/build` | **~1.57–1.73 s** (median ~1.60 s) |
| `POST /v1/events/postflight` (new) | ~17 ms |
| `POST /v1/events/postflight` (duplicate) | **~0.6 ms** |
| `POST /v1/memory/search` (SQL-filtered) | ~102 ms |

Preflight breakdown:

| Stage | Time |
|---|---|
| `vector.query` — pure-Python cosine over 20,451 rows | **1454 ms** |
| `graph.query` | 23 ms |
| `vector.search` (decisions, SQL-filtered) | 16 ms |
| `vector.search` (tasks, SQL-filtered) | 15 ms |

Within the ≤5 s p95 target, but **89% of preflight is the pre-existing brute-force
cosine scan** in `core/vector_store.query`. It re-parses every row's JSON
embedding on each call and is O(rows). This was not touched (out of scope: the
task says extend, not rewrite). It is the obvious next optimisation — candidate
pre-filtering, a cached matrix, or an ANN index — and it will get worse as memory
grows.

## 4b. Read-only proof, and why the hash later changed

Immediately around `bash run_cortex.sh --from-hermes` (21:00 → 21:03):

| | value |
|---|---|
| size before / after | 281,407,488 / 281,407,488 (unchanged) |
| mtime before / after | identical |
| sha256 before / after | `1b8c8802…6967c` / `1b8c8802…6967c` (**identical**) |

A later re-check (21:25) showed the **same size but a different sha256**. That
is *not* Cortex writing. The Hermes desktop app and agent were running and
Faisal was actively chatting: 10 new message rows were written between 21:03 and
21:09 (verified by reading `messages` with `timestamp > 21:03` — they are live
chat turns in Faisal's own voice).

Cortex's handle cannot write, and this is enforced by SQLite, not by our care:

```
CREATE TABLE lekiu_should_fail (x INT)   -> attempt to write a readonly database
INSERT INTO messages ...                 -> attempt to write a readonly database
```

Takeaway for future verification: on a machine where Hermes is live, assert the
read-only contract with **size + the `mode=ro` write rejection**, and treat a
hash change as meaningful only when Hermes is stopped.

## 5. Operational warning: `run_cortex.sh` wipes API-ingested memory

`run_cortex.sh` does `rm -f data/vector_store.db data/graph_store.db` before
re-curating (pre-existing behaviour, unchanged per the task). Memory ingested via
`/v1/events/postflight` lives in those same files, so **running `run_cortex.sh`
destroys it**. A running API server keeps writing to the deleted inode until
restarted, which makes the loss non-obvious.

Do not run `run_cortex.sh` against a live service. Consolidating the demo
pipeline and the service store is a follow-up decision for Jebat.

## 6. Known cosmetic issue (pre-existing, not introduced)

`python3 -W error::ResourceWarning -m unittest tests.test_context_builder` emits
`ResourceWarning: unclosed database`. Cause: `ContextBuilder.close()` only closes
stores it owns (`_owns_stores`), and those tests inject stores; Python 3.14 now
reports the unclosed handles loudly. Present at `HEAD` before this work —
`git diff` shows no changes to `tests/` and none to the `_owns_stores` logic.
Left alone deliberately; fixing it means changing existing test ownership
semantics, which is out of scope for this task.
