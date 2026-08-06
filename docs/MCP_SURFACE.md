# MCP Surface — Jebat-Cortex (RC1 / v0.4)

Design doc for Task2 Workstream 6. Exposes Cortex as a **local stdio MCP server**
so Jebat, Lekiu, Claude Code and Codex can pull memory/context directly. Tuah's
mandatory preflight uses the internal HTTP API (Workstream 8), not MCP.

## 1. Tools

| Tool | Purpose | Maps to |
|---|---|---|
| `cortex.search_memory` | filtered memory search | `POST /v1/memory/search` |
| `cortex.build_context` | build preflight digest for a prompt | `POST /v1/context/build` |
| `cortex.get_project_state` | project objective/decisions/tasks | `GET /v1/projects/{id}/state` |
| `cortex.get_decision_history` | approved decisions for project | memory search `type=decision&status=approved` |
| `cortex.get_related_experiences` | graph neighbours for an entity | graph store query |
| `cortex.record_outcome` | submit a postflight event | `POST /v1/events/postflight` |
| `cortex.health` | service liveness + identity | `GET /v1/health` |

No destructive admin tools are exposed via MCP by default (Task2 Workstream 6:
"Do not expose destructive administration functions through MCP by default").

## 2. Transport

- `mcp/server.py` runs as a stdio MCP server (the `mcp` optional dependency from
  `graphifyy`'s tree, or a minimal stdio JSON-RPC implementation — whichever
  keeps dependencies small). It imports the same `core/*` modules the HTTP API
  uses; no logic duplication.
- Binds to localhost only (inherits the service's localhost posture).
- Emits the same provenance/status fields as the HTTP API.

## 3. Configuration examples

**Claude Code** (`~/.claude.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
    "jebat-cortex": {
      "command": "/Users/faisal/dev/projects/the-magnificent-5/jebat-cortex/.venv-graphify/bin/python",
      "args": ["-m", "mcp.server"],
      "cwd": "/Users/faisal/dev/projects/the-magnificent-5/jebat-cortex"
    }
  }
}
```

> Use a dedicated runtime venv for the service (Python 3.11+). Graphify's venv
> is separate; do not co-mingle.

**Codex** (where supported, stdio):

```json
{
  "mcpServers": {
    "jebat-cortex": {
      "command": "python3.11",
      "args": ["-m", "mcp.server"],
      "cwd": "/Users/faisal/dev/projects/the-magnificent-5/jebat-cortex"
    }
  }
}
```

**Generic stdio client**: spawn `python3.11 -m mcp.server` with cwd at the
repo root; the server reads `config/cortex_rules.json` for limits/identity.

## 4. Behaviour contract

- `cortex.build_context` must fail loud (not silent) if the service's embedding
  identity mismatches the store — same `check_compatibility(raise_on_mismatch=True)`
  guard as the HTTP preflight.
- `cortex.record_outcome` is idempotent on `request_id`.
- Tools never execute shell, never commit/push, never open Hermes `state.db`
  writable.
- If Cortex is down, the MCP client should report `memory_status=degraded` and
  let the agent continue with session context only (Workstream 8 degraded mode).

## 5. Files (Phase 3)

- `mcp/server.py` — stdio MCP server over `core/*` + `api/` routers.
- `docs/MCP_INTEGRATION.md` — the user-facing version of this design (deliverable).
