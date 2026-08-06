# TASK 6 — GA Hardening: Solidness (Phase 4 / pre-GA)

You are **Lekiu** (Claude Code), Builder under Jebat/Hermes supervision.
This task makes RC1 SOLID before general availability (GA). No new features —
hardening only. Source of truth: `core/vector_store.py`, `core/graph_store.py`,
`core/memory_curator.py`, `core/context_builder.py`, `api/service.py`,
`mcp/server.py`, `tests/`, `service/launchd/*.plist`, `docs/`. Read first.

## Context
RC1 froze with one known Low: `ContextBuilder` does not track vector/graph
ownership separately, and the suite emits `ResourceWarning` for unclosed SQLite
connections. Codex flagged it as non-blocking but Faisal requires it SOLID before
GA — "no half-baked". Fix it properly; do not paper over with `filterwarnings`.

## 1. Eliminate ResourceWarning at the source (PRIORITY)

The warning means a store opens `sqlite3.connect` but is never explicitly
`close()`-d before it is garbage-collected. Locations to harden:

- `core/vector_store.py`, `core/graph_store.py`, `core/memory_curator.py`:
  - Ensure `close()` closes `self.conn` idempotently (guard `if self.conn:`).
  - Add a `__del__` fallback that calls `close()` ONLY if the connection is
    still open (best-effort, never raises). This catches GC-time warnings.
  - If the class is used as a context manager anywhere, ensure `__enter__/
    __exit__` exist; otherwise leave it.
- `api/service.py` (`CortexService`): it holds `vector_store`, `graph_store`,
  `queue`. `CortexService.close()` must close ALL three. Audit that every code
  path that builds a `CortexService` (app factory, tests, CLI) eventually calls
  `close()` — prefer `try/finally` or a context manager in `cli/` and tests.
- `core/context_builder.py`: it receives `vector_store` + `graph_store` (the
  ownership tracking task below). Add explicit `close()` that closes both, and a
  `__del__` best-effort. Track the refs (see #2).
- `mcp/server.py` (`MCPServer`): closes its service in `close()` — ensure the
  stdio loop calls it on exit (finally).
- **Tests**: every test that creates `VectorStore(":memory:")`,
  `GraphStore(":memory:")`, `EventQueue(...)`, `CortexService(...)`,
  `CortexApp(...)`, `MCPServer(...)` MUST call `.close()` in `tearDown` (or use
  a context manager). This is the bulk of the warning source. Add a helper or
  use `self.addCleanup(obj.close)` so it cannot be forgotten.

**Acceptance:** running the FULL suite with `python3 -W error::ResourceWarning
-m unittest discover -s tests` must pass with ZERO `ResourceWarning` errors. This
is the proof, not a grep.

## 2. ContextBuilder ownership tracking

`core/context_builder.py` currently takes stores but does not record which
object owns the vector vs graph connection. Add an explicit `own_stores` /
ownership attribute recording the `vector_store` and `graph_store` references it
holds, and close BOTH in `close()`. This makes the lifecycle auditable and stops
a store being dropped on the floor (which is what triggers the warning when the
builder is discarded).

## 3. Service operations documentation

Write `docs/SERVICE_OPERATIONS.md` covering, concretely:
- **Install**: `cp service/launchd/com.m5.jebat-cortex.plist ~/Library/LaunchAgents/`
  and `...-worker.plist`; set `CORTEX_HOME` + paths; `launchctl load` both.
- **The co-launch mechanism (tali ajaib)**: there are TWO launchd jobs
  (`serve` + `worker`), both `RunAtLoad` + `KeepAlive`. Document that BOTH must
  run — `serve` alone leaves the queue undrained. Verify both plists are valid
  XML and point at the correct venv/python + repo path.
- **Manual run** (debug): `python3 -m cli.cortex_cli serve --port 8765 &` then
  `python3 -m cli.cortex_cli worker &`.
- **Stop / restart**: `launchctl unload` / `load`; or `kill` the PIDs.
- **Health check**: `curl -s http://127.0.0.1:8765/v1/health`.
- **Troubleshoot**: queue not draining → is worker running? 409 embedding
  mismatch → rebuild store with matching embedder. 403 on admin → check
  `CORTEX_ADMIN_TOKEN` + localhost. Degraded → a store failed to open.
- **Upgrade**: stop both jobs, pull, (re)load.
- **Logs**: where launchd writes stdout/stderr (StandardOutPath/StandardErrorPath).

Verify the two plists are well-formed XML (`plutil -lint` if available, else
`python3 -c "import plistlib; plistlib.load(open(path,'rb'))"`). Fix any path
that hard-codes another user's home (use `CORTEX_HOME` env + `~/` not absolute
`/Users/faisal`).

## Constraints
- NO new features. Hardening only.
- `~/.hermes/state.db` read-only (never opened writable).
- Localhost only; no shell exec; no commit/push.
- Keep 314 existing tests green (they must now ALSO pass under
  `-W error::ResourceWarning`).
- Python 3.9+ syntax.

## Verification (Jebat will re-run all of this)
1. `python3 -W error::ResourceWarning -m unittest discover -s tests` → **zero
   ResourceWarning, all green**. (This is the hard gate.)
2. `CortexService.close()` closes all three stores (unit test).
3. `ContextBuilder.close()` closes both vector + graph (unit test).
4. Both plists lint clean and use `CORTEX_HOME` (no hardcoded `/Users/faisal`).
5. `bash run_cortex.sh --from-hermes` → state.db UNCHANGED.
6. Live: load both plists (dry `launchctl` or at least validate), start serve+
   worker, health healthy, postflight → worker drains (queue_depth 0).

## Report format
Files changed; the `-W error::ResourceWarning` test result (actual count of
warnings = 0); close-discipline unit test results; plist lint output; state.db
read-only proof. Do NOT claim done without the zero-ResourceWarning evidence.
