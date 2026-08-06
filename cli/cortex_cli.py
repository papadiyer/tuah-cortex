"""CLI fallback: cortex health | build | search | serve | worker | mcp.

Runs the same CortexService the HTTP API and MCP server use, so it works with
the service stopped - useful when the launchd job is down or a store needs
inspecting.

Usage::

    python3 -m cli.cortex_cli health
    python3 -m cli.cortex_cli build "how does the queue work?" --budget 2000
    python3 -m cli.cortex_cli search --type decision --status approved
    python3 -m cli.cortex_cli serve --port 8765
    python3 -m cli.cortex_cli worker --once
    python3 -m cli.cortex_cli mcp
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict, List, Optional

from api.service import CortexService
from core.redact import redact_response


def _emit(payload: Any, as_json: bool = True) -> None:
    """Single stdout boundary for the CLI, redacted (P1-HIGH-1)."""
    safe = redact_response(payload)
    print(json.dumps(safe, indent=2, default=str) if as_json else safe)


def _cmd_health(args: argparse.Namespace) -> int:
    service = CortexService()
    try:
        status, body = service.health()
    finally:
        service.close()
    _emit(body)
    # Non-zero exit when degraded so shell callers can branch on it.
    return 0 if body.get("status") == "healthy" else 1


def _cmd_build(args: argparse.Namespace) -> int:
    service = CortexService()
    try:
        status, body = service.build_context(
            {
                "prompt": " ".join(args.prompt),
                "request_id": args.request_id,
                "project_hint": args.project,
                "token_budget": args.budget,
                "entrypoint": "cli",
            }
        )
    finally:
        service.close()

    if status != 200:
        _emit(body)
        return 2
    if args.markdown:
        # Raw digest text still goes through the redactor: --markdown bypasses
        # _emit, and it is the flag most likely to be piped into another tool.
        sys.stdout.write(redact_response(body.get("context_markdown", "")))
    else:
        _emit(body)
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    service = CortexService()
    try:
        status, body = service.search_memory(
            {
                "query": " ".join(args.query) if args.query else None,
                "filters": {
                    "project": args.project,
                    "memory_type": args.type,
                    "status": args.status,
                    "entity": args.entity,
                    "approved_only": args.approved_only,
                    "limit": args.limit,
                },
            }
        )
    finally:
        service.close()
    _emit(body)
    return 0 if status == 200 else 2


def _cmd_serve(args: argparse.Namespace) -> int:
    from api.app import serve

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    serve(args.host, args.port)
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    from workers.ingest_worker import main as worker_main

    forwarded: List[str] = []
    if args.once:
        forwarded.append("--once")
    return worker_main(forwarded)


def _cmd_mcp(args: argparse.Namespace) -> int:
    from mcp.server import main as mcp_main

    return mcp_main([])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex", description="Jebat-Cortex CLI")
    sub = parser.add_subparsers(dest="command")

    health = sub.add_parser("health", help="service + store health")
    health.set_defaults(func=_cmd_health)

    build = sub.add_parser("build", help="build a context digest for a prompt")
    build.add_argument("prompt", nargs="+")
    build.add_argument("--project", default=None, help="project hint")
    build.add_argument("--budget", type=int, default=None, help="token budget")
    build.add_argument("--request-id", default=None, help="request id (uuid)")
    build.add_argument("--markdown", action="store_true", help="print the digest only")
    build.set_defaults(func=_cmd_build)

    search = sub.add_parser("search", help="filtered memory search")
    search.add_argument("query", nargs="*")
    search.add_argument("--project", default=None)
    search.add_argument("--type", default=None, dest="type",
                        help="decision | experience | knowledge | task | artefact")
    search.add_argument("--status", default=None,
                        help="proposed | approved | rejected | completed | superseded")
    search.add_argument("--entity", default=None)
    search.add_argument("--approved-only", action="store_true", dest="approved_only")
    search.add_argument("--limit", type=int, default=20)
    search.set_defaults(func=_cmd_search)

    serve_cmd = sub.add_parser("serve", help="run the localhost HTTP API")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8765)
    serve_cmd.set_defaults(func=_cmd_serve)

    worker = sub.add_parser("worker", help="run the ingest worker")
    worker.add_argument("--once", action="store_true", help="drain due events then exit")
    worker.set_defaults(func=_cmd_worker)

    mcp_cmd = sub.add_parser("mcp", help="run the stdio MCP server")
    mcp_cmd.set_defaults(func=_cmd_mcp)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
