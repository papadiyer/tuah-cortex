# MCP Integration — Jebat-Cortex (RC1 / v0.4)

How to connect Claude Code, Codex or any stdio MCP client to Cortex memory.
This is the user-facing version of `docs/MCP_SURFACE.md`.

## 1. What you get

Seven read/append-only tools:

| Tool | Purpose |
|---|---|
| `cortex.health` | service liveness + embedding identity |
| `cortex.build_context` | tiered preflight digest for a prompt |
| `cortex.search_memory` | filtered search (project/type/status/entity/date) |
| `cortex.get_project_state` | objective, decisions, open tasks, artefacts |
| `cortex.get_decision_history` | approved decisions for a project |
| `cortex.get_related_experiences` | graph neighbours of a file/module/entity |
| `cortex.record_outcome` | submit a postflight event (idempotent) |

No destructive admin tools are exposed. The tools never execute shell commands,
never commit or push, and never open Hermes `state.db` writable.

## 2. Requirements

None beyond Python 3.9+. The server is stdlib-only — no `mcp` SDK, no FastAPI.

The HTTP service does **not** need to be running: the MCP server talks to the
same `core/*` stores directly.

## 3. Configuration

### Claude Code (`.mcp.json` in the repo, or `~/.claude.json`)

```json
{
  "mcpServers": {
    "jebat-cortex": {
      "command": "python3",
      "args": ["-m", "mcp.server"],
      "cwd": "/Users/faisal/dev/projects/the-magnificent-5/jebat-cortex",
      "env": {
        "PYTHONPATH": "/Users/faisal/dev/projects/the-magnificent-5/jebat-cortex"
      }
    }
  }
}
```

> **Set `PYTHONPATH` explicitly.** This machine exports an ambient `PYTHONPATH`
> pointing at Hermes' Python 3.11 venv while `python3` is 3.14; overriding it in
> `env` keeps the server importing this repo's modules. See
> `docs/RUNTIME_NOTES.md` §1.

### Codex / generic stdio client

Same shape: run `python3 -m mcp.server` with `cwd` at the repo root.

## 4. Verify it works

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"cortex.health","arguments":{}}}' \
  | python3 -m mcp.server 2>/dev/null
```

Expected: a JSON-RPC result whose content block reports `"status": "healthy"`
and the embedding identity, e.g.

```json
{"status": "healthy", "version": "0.4.0", "graph_store": "ready",
 "vector_store": "ready", "embedding_identity": "deterministic",
 "identity": {"backend": "deterministic", "model": "deterministic", "dim": 512}}
```

## 5. Behaviour contract

- **Fail loud, never silent.** If the configured embedder does not match the
  store's identity, `cortex.build_context` and `cortex.search_memory` return an
  MCP tool error (`isError: true`) carrying `embedding_identity_mismatch` —
  they do not return empty or partial memory.
- **Degraded mode.** If a store is unavailable the tool errors with `degraded`.
  The agent should report `memory_status=degraded` and continue on session
  context only — it must **not** fabricate memory.
- **Idempotency.** `cortex.record_outcome` is keyed on `request_id`; a repeat
  returns the original `event_id` with `queued: false`.
- **Status honesty.** A `proposed` decision is never returned as `approved`.
- **Tier 2 is on demand.** Full conversations and historical detail are only
  reachable through `cortex.search_memory`, never auto-injected into a digest.

## 6. Notes

- Events recorded via `cortex.record_outcome` are queued; run the ingest worker
  (`python3 -m workers.ingest_worker`) for them to become searchable memory.
- The local package is named `mcp` to match the documented
  `python3 -m mcp.server` invocation. If the upstream `mcp` SDK is ever
  installed into the same environment, rename this package or launch by path to
  avoid the import collision.
