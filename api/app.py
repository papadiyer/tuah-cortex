"""HTTP app factory and localhost-only server.

FastAPI is not installed in this runtime (see docs/RUNTIME_NOTES.md), so this is
the stdlib ``http.server`` fallback the task allows: a minimal router, no
third-party dependency, no ASGI layer. The routing table mirrors
API_CONTRACTS.md exactly, and every handler delegates to CortexService, so the
same behaviour is available over MCP and the CLI.

Security posture:
  * binds 127.0.0.1 only - ``create_server`` refuses a non-loopback host;
  * request bodies are capped at ``service.max_body_bytes`` (256k);
  * no shell execution anywhere in the request path;
  * admin routes need ``CORTEX_ADMIN_TOKEN``;
  * logs are routed through the redaction helper.

Run as::

    python3 -m api.app --port 8765
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Pattern, Tuple

from api import admin as admin_routes
from api import context as context_routes
from api import events as events_routes
from api import health as health_routes
from api import memory as memory_routes
from api import projects as projects_routes
from api.models import error_body
from api.service import CortexService
from core.redact import lekiu_redact, redact_response
from core.rules import SERVICE_VERSION, load_rules

LOGGER = logging.getLogger("cortex.http")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

Handler = Callable[..., Tuple[int, Dict[str, Any]]]


class Route:
    """One routing entry: method + compiled path pattern -> handler."""

    def __init__(
        self,
        method: str,
        path: str,
        handler: Handler,
        needs_body: bool,
        needs_token: bool,
        needs_peer: bool = False,
    ):
        self.method = method.upper()
        self.path = path
        self.handler = handler
        self.needs_body = needs_body
        self.needs_token = needs_token
        # Admin routes are localhost-bound and need the caller's address.
        self.needs_peer = needs_peer
        self.pattern: Pattern[str] = re.compile(
            "^%s$" % re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path)
        )

    def match(self, method: str, path: str) -> Optional[Dict[str, str]]:
        if method.upper() != self.method:
            return None
        found = self.pattern.match(path)
        return found.groupdict() if found else None


def build_routes(service: CortexService) -> List[Route]:
    """Assemble the routing table from the individual router modules."""
    return [
        Route("GET", "/v1/health", lambda: health_routes.get_health(service), False, False),
        Route(
            "POST",
            "/v1/context/build",
            lambda body: context_routes.post_context_build(service, body),
            True,
            False,
        ),
        Route(
            "POST",
            "/v1/events/postflight",
            lambda body: events_routes.post_postflight(service, body),
            True,
            False,
        ),
        # Read-back must be registered after postflight: "postflight" would
        # otherwise be captured as an {event_id} on the POST route too.
        Route(
            "GET",
            "/v1/events/{event_id}",
            lambda event_id: events_routes.get_event(service, event_id),
            False,
            False,
        ),
        Route(
            "POST",
            "/v1/memory/search",
            lambda body: memory_routes.post_memory_search(service, body),
            True,
            False,
        ),
        Route(
            "GET",
            "/v1/projects/{project_id}/state",
            lambda project_id: projects_routes.get_project_state(service, project_id),
            False,
            False,
        ),
        Route(
            "GET",
            "/v1/admin/queue",
            lambda token, peer=None: admin_routes.get_queue(service, token, peer),
            False,
            True,
            needs_peer=True,
        ),
        Route(
            "POST",
            "/v1/admin/reindex",
            lambda token, peer=None, body=None: admin_routes.post_reindex(service, token, peer),
            True,
            True,
            needs_peer=True,
        ),
        # P1-HIGH-2: the only route that can mint an approved decision.
        Route(
            "POST",
            "/v1/admin/decision/{decision_id}/approve",
            lambda decision_id, token, peer=None, body=None: admin_routes.post_decision_approve(
                service, decision_id, token, peer, body
            ),
            True,
            True,
            needs_peer=True,
        ),
    ]


class CortexApp:
    """Framework-free application: dispatch(method, path, body, headers)."""

    def __init__(self, service: Optional[CortexService] = None, rules: Optional[dict] = None):
        self.rules = rules or load_rules()
        self.service = service if service is not None else CortexService(rules=self.rules)
        self._owns_service = service is None
        self.routes = build_routes(self.service)
        self.max_body_bytes = int(self.rules.get("service", {}).get("max_body_bytes", 262144))
        # The server is threaded (one thread per connection, needed for HTTP/1.1
        # keep-alive), but the SQLite connections behind CortexService are single
        # writer objects shared by every request. Serialise handler execution so
        # two threads never touch one connection at the same time. Requests are
        # short local reads/writes, so the throughput cost is negligible next to
        # the correctness guarantee.
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._owns_service:
            self.service.close()

    def dispatch(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        peer: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Route one request. Returns (status, body). Never raises.

        ``peer`` is the client's IP as seen by the socket. It is supplied by the
        HTTP layer and cannot be set by a header, so a remote caller cannot
        forge it to reach a localhost-only admin route. In-process callers
        (tests, CLI) default to loopback.
        """
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        path = (path or "/").split("?", 1)[0].rstrip("/") or "/"
        if peer is None:
            peer = DEFAULT_HOST

        matched_path = False
        for route in self.routes:
            params = route.match(method, path)
            if params is None:
                # Track "path exists but method differs" for a correct 405.
                if route.pattern.match(path):
                    matched_path = True
                continue

            kwargs: Dict[str, Any] = dict(params)
            if route.needs_token:
                kwargs["token"] = _bearer_token(headers)
            if route.needs_peer:
                kwargs["peer"] = peer
            if route.needs_body:
                kwargs["body"] = body if body is not None else {}

            try:
                with self._lock:
                    return route.handler(**kwargs)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.error("unhandled error on %s %s: %s", method, path, lekiu_redact(str(exc))[:300])
                return 500, error_body("internal_error", "unexpected server error")

        if matched_path:
            return 405, error_body("method_not_allowed", "%s not allowed on %s" % (method, path))
        return 404, error_body("not_found", "no route for %s %s" % (method, path))


def _bearer_token(headers: Dict[str, str]) -> Optional[str]:
    """Extract the admin token from the accepted header forms.

    ``X-Cortex-Admin-Token`` is the documented header for the decision-approve
    endpoint; ``Authorization: Bearer`` and ``X-Admin-Token`` are kept for the
    pre-existing admin routes.
    """
    explicit = headers.get("x-cortex-admin-token")
    if explicit:
        return explicit.strip()
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-admin-token") or None


def _make_request_handler(app: CortexApp):
    class CortexRequestHandler(BaseHTTPRequestHandler):
        server_version = "JebatCortex/%s" % SERVICE_VERSION
        protocol_version = "HTTP/1.1"

        # -- plumbing ------------------------------------------------------
        def log_message(self, fmt: str, *args: Any) -> None:
            # Default handler logs to stderr with the raw request line; route it
            # through the redactor and the service logger instead.
            LOGGER.info("%s - %s", self.address_string(), lekiu_redact(fmt % args))

        def _peer(self) -> str:
            """Client IP from the socket. Not caller-controllable."""
            try:
                return str(self.client_address[0])
            except Exception:  # pragma: no cover - defensive
                return ""

        def _respond(self, status: int, payload: Dict[str, Any]) -> None:
            # P1-HIGH-1: last redaction gate before bytes leave the process. The
            # routers redact too; this is the backstop that makes it impossible
            # for a new route to serialise an unredacted body by omission.
            payload = redact_response(payload)
            raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            # Localhost-only service; no CORS, no caching of memory content.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _read_body(self) -> Tuple[bool, Any]:
            length_header = self.headers.get("Content-Length") or "0"
            try:
                length = int(length_header)
            except ValueError:
                return False, error_body("invalid_request", "bad Content-Length")
            if length < 0:
                return False, error_body("invalid_request", "bad Content-Length")
            if length > app.max_body_bytes:
                return False, error_body(
                    "payload_too_large",
                    "body exceeds %d bytes" % app.max_body_bytes,
                )
            if length == 0:
                return True, {}
            raw = self.rfile.read(length)
            try:
                return True, json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return False, error_body("invalid_request", "body must be valid JSON")

        def _headers_dict(self) -> Dict[str, str]:
            return {k: v for k, v in self.headers.items()}

        # Cap on how much of an over-limit body we will read purely to keep the
        # socket clean. Beyond this the connection is reset instead.
        _DRAIN_LIMIT = 4 * 1024 * 1024

        def _drain_body(self) -> None:
            """Consume the request body so the error response can be delivered."""
            try:
                remaining = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            if remaining <= 0 or remaining > self._DRAIN_LIMIT:
                return  # unreasonably large: let the close reset the connection
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)

        # -- verbs ---------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            status, payload = app.dispatch(
                "GET", self.path, None, self._headers_dict(), peer=self._peer()
            )
            self._respond(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            ok, body = self._read_body()
            if not ok:
                too_large = body.get("error") == "payload_too_large"
                if too_large:
                    # The oversized body is still sitting in the socket. Two
                    # things go wrong if we just close:
                    #   * left unread on a keep-alive connection, those bytes
                    #     are parsed as the next request line (spurious 414);
                    #   * closing with unread data makes the OS send a TCP RST,
                    #     which can destroy the 413 before the client reads it.
                    # So: drain a bounded amount so the response is delivered,
                    # and only reset if the sender is truly unreasonable.
                    self.close_connection = True
                    self._drain_body()
                    self.send_response(413)
                    raw = json.dumps(redact_response(body)).encode("utf-8")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(raw)
                    try:
                        self.wfile.flush()
                    except OSError:
                        pass
                    return
                self._respond(400, body)
                return
            status, payload = app.dispatch(
                "POST", self.path, body, self._headers_dict(), peer=self._peer()
            )
            self._respond(status, payload)

    return CortexRequestHandler


class _LocalhostServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        """Bind without the stdlib's reverse-DNS lookup.

        ``HTTPServer.server_bind`` calls ``socket.getfqdn(host)`` to populate
        ``server_name``. On a host whose resolver has no fast answer for
        127.0.0.1 that lookup blocks for ~35s on every startup (measured on this
        machine), which would stall the launchd job and every test that opens a
        socket. We bind loopback only, so the FQDN is knowable without asking
        the resolver.
        """
        # Skip HTTPServer.server_bind; call its parent (socketserver.TCPServer).
        from socketserver import TCPServer

        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = "localhost"
        self.server_port = port


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    app: Optional[CortexApp] = None,
) -> Tuple[_LocalhostServer, CortexApp]:
    """Create the HTTP server bound to a loopback address only.

    A non-loopback host is refused outright: the service must never be exposed
    publicly (task constraint 4), and that guarantee should not depend on the
    operator passing the right flag.
    """
    try:
        resolved = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(
            "refusing to bind %r: host must be a loopback IP literal (127.0.0.1 or ::1)" % host
        )
    if not resolved.is_loopback:
        raise ValueError("refusing to bind non-loopback address %s; localhost only" % host)

    application = app if app is not None else CortexApp()
    server = _LocalhostServer((host, int(port)), _make_request_handler(application))
    if resolved.version == 6:  # pragma: no cover - platform dependent
        server.address_family = socket.AF_INET6
    return server, application


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Blocking server loop."""
    rules = load_rules()
    cfg = rules.get("service", {})
    host = host or cfg.get("host", DEFAULT_HOST)
    port = int(port or cfg.get("port", DEFAULT_PORT))

    server, application = create_server(host, port)
    LOGGER.info("Jebat-Cortex %s listening on http://%s:%d (localhost only)", SERVICE_VERSION, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        application.close()


def create_app(service: Optional[CortexService] = None) -> CortexApp:
    """App factory (importable by tests and alternative front ends)."""
    return CortexApp(service=service)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Jebat-Cortex HTTP API (localhost only)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="loopback address to bind")
    parser.add_argument("--port", type=int, default=None, help="port (default 8765)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    serve(args.host, args.port or 0 or DEFAULT_PORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
