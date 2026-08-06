# Jebat-Cortex — Service Operations

Operational runbook for the persistent cognitive runtime (v0.4.0). Covers
install, the two-job co-launch, health checks, troubleshooting and upgrade.

Audience: whoever has to keep Cortex running — normally Faisal or Jebat/Hermes.

---

## 1. The co-launch mechanism ("tali ajaib")

**Jebat-Cortex is TWO processes. Both must run.**

| Job | Label | What it does |
|---|---|---|
| API | `com.m5.jebat-cortex` | Serves `/v1/*` on `127.0.0.1:8765`. Builds context, answers searches, **enqueues** postflight events. |
| Worker | `com.m5.jebat-cortex-worker` | Claims queued events and curates them into the vector + graph stores. |

The API **never ingests inline**. `POST /v1/events/postflight` commits the event
to a durable SQLite queue and returns `202` immediately — that is what keeps the
request fast and crash-safe. Turning that row into memory is the worker's job.

> **Running `serve` alone leaves the queue undrained.** The API keeps answering
> and keeps returning `202`, so nothing looks broken — but `queue_depth` in
> `/v1/health` climbs forever and no new memory is ever written. This is the
> single most likely operational failure, and it is silent from the caller's
> side. Always load both jobs.

Both plists set `RunAtLoad` (start immediately on load / login) and `KeepAlive`
with `SuccessfulExit=false` (restart if the process dies unexpectedly), with
`ThrottleInterval=10` so a crash-loop cannot spin hot.

Verify both are loaded:

```bash
launchctl list | grep jebat-cortex
# com.m5.jebat-cortex
# com.m5.jebat-cortex-worker      <-- if this line is missing, the queue is not draining
```

---

## 2. Install

Per-user LaunchAgents. No `sudo`, no system-wide daemon.

```bash
cd ~/dev/projects/the-magnificent-5/jebat-cortex

cp service/launchd/com.m5.jebat-cortex.plist        ~/Library/LaunchAgents/
cp service/launchd/com.m5.jebat-cortex-worker.plist ~/Library/LaunchAgents/

launchctl load ~/Library/LaunchAgents/com.m5.jebat-cortex.plist
launchctl load ~/Library/LaunchAgents/com.m5.jebat-cortex-worker.plist

curl -s http://127.0.0.1:8765/v1/health
```

### Paths and `CORTEX_HOME`

**launchd does not expand `~` or `$VAR`** in `WorkingDirectory`,
`StandardOutPath` or `ProgramArguments`. Those keys are taken literally, which
is why the old plists hardcoded `/Users/faisal/...` and broke for anyone else.

Both plists now invoke `/bin/sh -c` and resolve paths *inside the shell*, which
does expand. No home directory is hardcoded. Defaults:

| Variable | Default | Meaning |
|---|---|---|
| `CORTEX_HOME` | `$HOME/dev/projects/the-magnificent-5/jebat-cortex` | Repo root; also `PYTHONPATH` and the `cd` target |
| `CORTEX_PYTHON` | `python3` (from `PATH`) | Interpreter — point at a venv if you use one |
| `CORTEX_PORT` | `8765` | API port (serve job only) |

To override, set them **before** loading (or unload/load afterwards) — launchd
jobs do not inherit your interactive shell's environment:

```bash
launchctl setenv CORTEX_HOME   /path/to/jebat-cortex
launchctl setenv CORTEX_PYTHON /path/to/venv/bin/python3
launchctl unload ~/Library/LaunchAgents/com.m5.jebat-cortex.plist
launchctl load   ~/Library/LaunchAgents/com.m5.jebat-cortex.plist
```

> `launchctl setenv` is required because launchd starts jobs from a minimal
> environment. If Cortex runs under a virtualenv, set `CORTEX_PYTHON` — the
> bare `python3` on launchd's `PATH` is usually the system interpreter.

### Validate the plists before loading

```bash
plutil -lint service/launchd/com.m5.jebat-cortex.plist \
             service/launchd/com.m5.jebat-cortex-worker.plist
```

Without `plutil`:

```bash
python3 -c "import plistlib,sys; [plistlib.load(open(p,'rb')) for p in sys.argv[1:]]; print('OK')" \
  service/launchd/com.m5.jebat-cortex.plist \
  service/launchd/com.m5.jebat-cortex-worker.plist
```

Both must report OK. A malformed plist fails silently at load time — launchd
logs it and simply never starts the job.

---

## 3. Manual run (debug)

When you want logs on your terminal instead of via launchd:

```bash
cd ~/dev/projects/the-magnificent-5/jebat-cortex
export PYTHONPATH="$PWD"

python3 -m cli.cortex_cli serve --port 8765 &   # API
python3 -m cli.cortex_cli worker &              # worker — do not skip this

curl -s http://127.0.0.1:8765/v1/health
```

Drain once and exit (useful after a backlog builds up):

```bash
python3 -m cli.cortex_cli worker --once
```

Stop the manual pair:

```bash
kill %1 %2      # or: pkill -f cli.cortex_cli ; pkill -f workers.ingest_worker
```

Do not run the manual pair and the launchd jobs at the same time — the second
`serve` will fail to bind port 8765, and two workers will contend for the queue
(safe, thanks to `BEGIN IMMEDIATE`, but pointless).

---

## 4. Stop / restart

```bash
# Stop both
launchctl unload ~/Library/LaunchAgents/com.m5.jebat-cortex.plist
launchctl unload ~/Library/LaunchAgents/com.m5.jebat-cortex-worker.plist

# Start both
launchctl load ~/Library/LaunchAgents/com.m5.jebat-cortex.plist
launchctl load ~/Library/LaunchAgents/com.m5.jebat-cortex-worker.plist

# Restart just the worker
launchctl unload ~/Library/LaunchAgents/com.m5.jebat-cortex-worker.plist
launchctl load   ~/Library/LaunchAgents/com.m5.jebat-cortex-worker.plist
```

`KeepAlive` restarts a job that *crashes*, so `kill <pid>` is not a stop — it is
a restart. Use `launchctl unload` to actually stop a job.

Both processes shut down cleanly: the worker handles `SIGTERM`/`SIGINT` and
finishes its current event, and every store connection is closed on exit. An
event interrupted mid-flight stays `processing` and is reset to `queued` by the
next worker boot, so nothing is stranded.

---

## 5. Health check

```bash
curl -s http://127.0.0.1:8765/v1/health | python3 -m json.tool
```

Healthy response:

```json
{
  "status": "healthy",
  "version": "0.4.0",
  "graph_store": "ready",
  "vector_store": "ready",
  "identity": {"backend": "deterministic", "model": "deterministic", "dim": 512},
  "queue_depth": 0,
  "last_ingestion": "2026-08-06T16:34:26+00:00",
  "memory_status": "ready"
}
```

What to read:

| Field | Meaning |
|---|---|
| `status` | `healthy` or `degraded`. Degraded still returns HTTP 200 — read `problems`. |
| `queue_depth` | Events waiting. Should return to 0. Persistently rising ⇒ **worker is not running**. |
| `last_ingestion` | Timestamp of the last drained event. `null` long after a postflight ⇒ worker never ran. |
| `identity` | Active embedding backend/model/dim. Must match what the store was built with. |
| `problems` | Present only when degraded; names the failing component. |

Health is deliberately `200` even when degraded so a monitor can read the
detail. Endpoints that cannot serve correctly still fail with `503`.

---

## 6. Troubleshooting

### Queue not draining (`queue_depth` keeps rising)

The worker is not running. This is the "tali ajaib" failure.

```bash
launchctl list | grep jebat-cortex-worker    # missing? load it
launchctl load ~/Library/LaunchAgents/com.m5.jebat-cortex-worker.plist
tail -50 logs/cortex-worker.err.log
python3 -m cli.cortex_cli worker --once      # drain the backlog now
```

Nothing is lost while the worker is down — events are committed durably before
the API returns `202`, and are drained whenever the worker comes back.

### `409 embedding_identity_mismatch`

The vector store holds rows embedded with a different backend/model/dimension
than the one now configured. Cortex **fails loud** instead of silently dropping
that memory from retrieval.

```bash
python3 -m cli.cortex_cli health     # compare identity against the store
```

Fix by switching the embedder config back to what built the store, or rebuild
the store with the embedder you now want. Do not mix identities in one database
— cosine scores across embedding spaces are meaningless. Rebuilding rewrites
memory, so it needs Faisal's approval.

### `403` on an admin route

Admin routes (`/v1/admin/*`) require **both** a token and a loopback caller.

- `admin_disabled` (403) → `CORTEX_ADMIN_TOKEN` is not set. It is deliberately
  absent from the plists: a secret must never be committed. Set it with
  `launchctl setenv CORTEX_ADMIN_TOKEN <token>` and reload the job.
- `forbidden` (403) → the call did not come from `127.0.0.1`/`::1`.
- `unauthorized` (401) → wrong token.

Pass it as `Authorization: Bearer <token>`. Admin routes are never exposed over
MCP.

### `status: degraded`

A store failed to open. Read `problems`:

- `vector_store` / `graph_store` — the database could not be opened (missing
  directory, permissions, disk full, corrupt file).
- `queue` — the queue database could not be opened.
- `embedding_identity_mismatch` — see above.

While degraded, `/v1/context/build` returns `503` rather than a partial digest,
and the worker refuses to ingest — Cortex never fabricates memory or quietly
writes into the wrong database.

```bash
tail -50 logs/cortex-api.err.log
ls -la data/
```

### Port 8765 already in use

Another `serve` is running (often a manual one alongside launchd):

```bash
pkill -f "cli.cortex_cli serve"
# or move the service: launchctl setenv CORTEX_PORT 8766 && reload
```

---

## 7. Upgrade

```bash
launchctl unload ~/Library/LaunchAgents/com.m5.jebat-cortex.plist
launchctl unload ~/Library/LaunchAgents/com.m5.jebat-cortex-worker.plist

cd ~/dev/projects/the-magnificent-5/jebat-cortex
git pull

python3 -m unittest discover -s tests        # must be green before reloading

# If the plists changed, re-copy them:
cp service/launchd/*.plist ~/Library/LaunchAgents/

launchctl load ~/Library/LaunchAgents/com.m5.jebat-cortex.plist
launchctl load ~/Library/LaunchAgents/com.m5.jebat-cortex-worker.plist

curl -s http://127.0.0.1:8765/v1/health
```

Stop the worker **before** pulling, so no event is mid-ingest while the code
changes underneath it. Store schemas migrate additively at open time, so an
upgrade does not require a rebuild. Memory is never destroyed by an upgrade;
`/v1/admin/reindex` is a non-destructive scan only.

---

## 8. Logs

Both jobs redirect stdout and stderr from inside their shell wrapper (append
mode, so history survives a restart):

| File | Contents |
|---|---|
| `logs/cortex-api.out.log` | API stdout |
| `logs/cortex-api.err.log` | API log lines — startup banner, per-request lines, errors |
| `logs/cortex-worker.out.log` | Worker stdout |
| `logs/cortex-worker.err.log` | Worker log lines — ingest results, retries, dead letters |

Python logging writes to **stderr**, so the `.err.log` files carry normal
operational output, not just failures.

```bash
tail -f logs/cortex-api.err.log logs/cortex-worker.err.log
```

Paths are relative to `CORTEX_HOME`. The wrapper runs `mkdir -p logs` before
redirecting, so a fresh checkout does not need the directory pre-created. Log
files are gitignored. All log lines pass through the redactor — payload text
and secrets are not written verbatim.

Dead-lettered events (past `queue_max_attempts`) are retained, not dropped:

```bash
curl -s -H "Authorization: Bearer $CORTEX_ADMIN_TOKEN" \
  http://127.0.0.1:8765/v1/admin/queue | python3 -m json.tool
```

---

## 9. Post-change verification

After any change to the service, plists or stores:

```bash
# 1. Suite green, and no leaked SQLite handles
python3 -W error::ResourceWarning -m unittest discover -s tests

# 2. Plists valid
plutil -lint service/launchd/*.plist

# 3. Both jobs up
launchctl list | grep jebat-cortex

# 4. Health + drain
curl -s http://127.0.0.1:8765/v1/health | python3 -m json.tool
```

A postflight followed by `queue_depth` returning to `0` is the end-to-end proof
that both halves of the co-launch are working.
