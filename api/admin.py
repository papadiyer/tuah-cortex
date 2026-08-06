"""Restricted admin routes — localhost + admin token only.

`POST /v1/admin/reindex` and `GET /v1/admin/queue`. These are deliberately NOT
exposed through MCP (MCP_SURFACE.md section 1) and never execute shell commands,
never commit/push, and never delete memory. Reindex is a non-destructive scan.

The token comes from the CORTEX_ADMIN_TOKEN environment variable. When it is
unset, admin routes answer 403 rather than defaulting to open.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, Optional, Tuple

from api.models import error_body
from api.service import CortexService
from core.redact import redacted_result

Result = Tuple[int, Dict[str, Any]]


def _auth(service: CortexService, token: Optional[str]) -> Optional[Result]:
    return service.authorise_admin(token)


def require_localhost(peer: Optional[str]) -> Optional[Result]:
    """Reject an admin call that did not come from the loopback interface.

    Defence in depth behind the loopback bind (P1-HIGH-2). The server already
    refuses to bind a public address, but a reverse proxy or port forward in
    front of it would present remote traffic as a local connection to the
    socket; the peer address is the last place that is still checkable.

    An unknown peer is refused rather than allowed: failing open here would make
    the whole control precisely as strong as the deployment topology, which is
    the assumption this check exists to remove.
    """
    address = (peer or "").strip()
    if not address:
        return 403, error_body(
            "forbidden", "admin endpoints require an identifiable local caller"
        )
    # Strip an IPv6 zone id and normalise the IPv4-mapped form (::ffff:127.0.0.1).
    address = address.split("%", 1)[0]
    try:
        resolved = ipaddress.ip_address(address)
    except ValueError:
        return 403, error_body("forbidden", "admin endpoints are localhost only")
    mapped = getattr(resolved, "ipv4_mapped", None)
    if mapped is not None:
        resolved = mapped
    if not resolved.is_loopback:
        return 403, error_body(
            "forbidden", "admin endpoints are localhost only; refused %s" % address
        )
    return None


def get_queue(service: CortexService, token: Optional[str], peer: Optional[str] = None) -> Result:
    denied = require_localhost(peer) or _auth(service, token)
    if denied is not None:
        return redacted_result(denied)
    return redacted_result(service.queue_stats())


def post_reindex(service: CortexService, token: Optional[str], peer: Optional[str] = None) -> Result:
    denied = require_localhost(peer) or _auth(service, token)
    if denied is not None:
        return redacted_result(denied)
    return redacted_result(service.reindex())


def post_decision_approve(
    service: CortexService,
    decision_id: str,
    token: Optional[str],
    peer: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Result:
    """Promote a decision ``proposed -> approved`` (P1-HIGH-2).

    The single authorised route to Tier 1 authority: admin token required,
    localhost only. Order matters - the origin is checked before the token so a
    remote caller cannot use this endpoint as a token oracle.
    """
    denied = require_localhost(peer) or _auth(service, token)
    if denied is not None:
        return redacted_result(denied)
    approver = str((body or {}).get("approved_by") or "admin")
    return redacted_result(service.approve_decision(decision_id, approver=approver))


ROUTES = (
    ("GET", "/v1/admin/queue", "get_queue"),
    ("POST", "/v1/admin/reindex", "post_reindex"),
    ("POST", "/v1/admin/decision/{decision_id}/approve", "post_decision_approve"),
)
