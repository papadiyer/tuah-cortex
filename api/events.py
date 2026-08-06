"""POST /v1/events/postflight — durable event intake.

The event is committed to the SQLite queue *before* 202 is returned; the ingest
worker curates it afterwards. No `&` background job is ever spawned here.
Idempotent on request_id: a repeat returns queued=false with the original
event_id.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from api.service import CortexService
from core.redact import redacted_result

Result = Tuple[int, Dict[str, Any]]


def post_postflight(service: CortexService, body: Any) -> Result:
    """Accept a postflight event.

    Unauthenticated by design (any local agent posts its outcome here), which is
    why a submitted decision status is never trusted: PostflightEvent.parse
    coerces 'approved' to 'proposed' before the event is queued, and the 202
    reports the downgrade.
    """
    return redacted_result(service.postflight(body))


def get_event(service: CortexService, event_id: str) -> Result:
    """Event lifecycle read-back: queued | processing | done | dead."""
    return redacted_result(service.event_state(event_id))


ROUTES = (
    ("POST", "/v1/events/postflight", "post_postflight"),
    ("GET", "/v1/events/{event_id}", "get_event"),
)
