"""Stdio MCP server over the Cortex service (MCP_SURFACE.md).

Implements the JSON-RPC 2.0 subset an MCP client needs — ``initialize``,
``tools/list``, ``tools/call``, ``ping`` — directly on stdin/stdout. The
upstream `mcp` SDK is not installed in this runtime (docs/RUNTIME_NOTES.md), and
the design explicitly allows "a minimal stdio JSON-RPC implementation -
whichever keeps dependencies small".

Seven tools, all read/append only:

    cortex.search_memory, cortex.build_context, cortex.get_project_state,
    cortex.get_decision_history, cortex.get_related_experiences,
    cortex.record_outcome, cortex.health

No destructive admin tools are exposed (MCP_SURFACE.md section 1). No shell
execution. Hermes state.db is never opened writable.

Protocol note: stdout carries framed JSON-RPC only. Every log line goes to
stderr, or it would corrupt the stream.

Run as::

    python3 -m mcp.server
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Callable, Dict, Optional, Tuple

from api.service import CortexService
from core.redact import lekiu_redact, redact_response
from core.rules import SERVICE_VERSION

LOGGER = logging.getLogger("cortex.mcp")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "jebat-cortex"

# JSON-RPC error codes (spec) plus an application-level code for tool failures.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

TOOLS = [
    {
        "name": "cortex.health",
        "description": "Service liveness, store state and embedding identity.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "cortex.build_context",
        "description": (
            "Build the tiered preflight context digest for a prompt. Fails loud on an "
            "embedding identity mismatch rather than returning degraded memory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The prompt to build context for"},
                "request_id": {"type": "string"},
                "project_hint": {"type": "string"},
                "token_budget": {"type": "integer", "description": "Advisory budget; hard caps still apply"},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cortex.search_memory",
        "description": "Filtered memory search (project, entity, type, status, date, confidence).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project": {"type": "string"},
                "entity": {"type": "string"},
                "memory_type": {
                    "type": "string",
                    "enum": ["decision", "experience", "knowledge", "task", "artefact"],
                },
                "status": {
                    "type": "string",
                    "enum": ["proposed", "approved", "rejected", "completed", "superseded"],
                },
                "approved_only": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "cortex.get_project_state",
        "description": "Project objective, approved/unresolved decisions, open tasks, artefacts.",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cortex.get_decision_history",
        "description": "Approved decisions for a project, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "cortex.get_related_experiences",
        "description": "Graph neighbours (relations) for an entity such as a file or module.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string"},
                "project": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["entity"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cortex.record_outcome",
        "description": (
            "Submit a postflight event; durably queued before returning. "
            "Idempotent on request_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "agent": {"type": "string"},
                "project": {"type": "string"},
                "prompt": {"type": "string"},
                "result_summary": {"type": "string"},
                "status": {"type": "string", "enum": ["completed", "failed"]},
                "decisions": {"type": "array", "items": {"type": "object"}},
                "lessons": {"type": "array", "items": {"type": "object"}},
                "open_tasks": {"type": "array", "items": {"type": "object"}},
                "artefacts": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["request_id"],
            "additionalProperties": False,
        },
    },
]


class MCPServer:
    """JSON-RPC dispatch over the shared CortexService."""

    def __init__(self, service: Optional[CortexService] = None):
        self._service = service
        self._owns_service = service is None

    @property
    def service(self) -> CortexService:
        # Lazily built so `initialize` answers instantly and a store problem
        # surfaces as a tool error rather than a startup crash.
        if self._service is None:
            self._service = CortexService()
        return self._service

    def close(self) -> None:
        if self._owns_service and self._service is not None:
            self._service.close()

    # -- tools -------------------------------------------------------------
    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        arguments = dict(arguments or {})
        handlers: Dict[str, Callable[[], Tuple[int, Dict[str, Any]]]] = {
            "cortex.health": lambda: self.service.health(),
            "cortex.build_context": lambda: self.service.build_context(
                {
                    "prompt": arguments.get("prompt"),
                    "request_id": arguments.get("request_id"),
                    "project_hint": arguments.get("project_hint"),
                    "token_budget": arguments.get("token_budget"),
                    "entrypoint": "mcp",
                }
            ),
            "cortex.search_memory": lambda: self.service.search_memory(
                {
                    "query": arguments.get("query"),
                    "filters": {
                        "project": arguments.get("project"),
                        "entity": arguments.get("entity"),
                        "memory_type": arguments.get("memory_type"),
                        "status": arguments.get("status"),
                        "approved_only": arguments.get("approved_only", False),
                        "limit": arguments.get("limit", 20),
                    },
                }
            ),
            "cortex.get_project_state": lambda: self.service.project_state(
                arguments.get("project_id", "")
            ),
            "cortex.get_decision_history": lambda: self.service.decision_history(
                project=arguments.get("project"), limit=int(arguments.get("limit", 20))
            ),
            "cortex.get_related_experiences": lambda: self.service.related_experiences(
                entity=arguments.get("entity", ""),
                project=arguments.get("project"),
                limit=int(arguments.get("limit", 10)),
            ),
            "cortex.record_outcome": lambda: self.service.postflight(arguments),
        }
        handler = handlers.get(name)
        if handler is None:
            return 404, {"error": "unknown_tool", "message": "no such tool: %s" % name}
        return handler()

    # -- JSON-RPC ----------------------------------------------------------
    def handle(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle one JSON-RPC message. Returns None for notifications."""
        if not isinstance(request, dict):
            return _error(None, INVALID_REQUEST, "request must be an object")

        method = request.get("method")
        req_id = request.get("id")
        is_notification = "id" not in request

        if not isinstance(method, str):
            return None if is_notification else _error(req_id, INVALID_REQUEST, "missing method")

        if method == "initialize":
            return _result(
                req_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVICE_VERSION},
                },
            )

        if method in ("notifications/initialized", "initialized"):
            return None

        if method == "ping":
            return _result(req_id, {})

        if method == "tools/list":
            return _result(req_id, {"tools": TOOLS})

        if method == "tools/call":
            params = request.get("params") or {}
            if not isinstance(params, dict):
                return _error(req_id, INVALID_PARAMS, "params must be an object")
            name = params.get("name")
            if not isinstance(name, str):
                return _error(req_id, INVALID_PARAMS, "params.name is required")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return _error(req_id, INVALID_PARAMS, "params.arguments must be an object")

            try:
                status, payload = self.call_tool(name, arguments)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.error("tool %s crashed: %s", name, lekiu_redact(str(exc))[:300])
                return _result(
                    req_id,
                    _tool_content({"error": "internal_error", "message": "tool failed"}, True),
                )

            # A non-2xx from the service (409 identity mismatch, 503 degraded)
            # is surfaced as an MCP tool error so the agent sees the failure
            # instead of silently receiving empty memory.
            return _result(req_id, _tool_content(payload, status >= 400))

        if is_notification:
            return None
        return _error(req_id, METHOD_NOT_FOUND, "unknown method: %s" % method)

    # -- transport ---------------------------------------------------------
    def serve_stdio(self, stdin=None, stdout=None) -> None:
        """Line-delimited JSON-RPC loop over stdin/stdout."""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                _write(stdout, _error(None, PARSE_ERROR, "invalid JSON"))
                continue

            response = self.handle(request)
            if response is not None:
                _write(stdout, response)


def _tool_content(payload: Dict[str, Any], is_error: bool) -> Dict[str, Any]:
    """MCP tool result envelope: JSON rendered as a text content block.

    P1-HIGH-1: every tool result is redacted here, at the one place where a
    payload becomes outbound text. Doing it per-tool would leave the next tool
    added to TOOLS leaking by default; doing it here means a tool cannot opt out.
    """
    return {
        "content": [
            {"type": "text", "text": json.dumps(redact_response(payload), indent=2, default=str)}
        ],
        "isError": bool(is_error),
    }


def _result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _write(stream, payload: Dict[str, Any]) -> None:
    stream.write(json.dumps(payload, default=str) + "\n")
    stream.flush()


def main(argv: Optional[list] = None) -> int:
    # stderr only: stdout is the JSON-RPC channel.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    server = MCPServer()
    try:
        server.serve_stdio()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
