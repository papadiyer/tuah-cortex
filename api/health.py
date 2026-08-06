"""GET /v1/health — liveness, store state, embedding identity, queue depth."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from api.service import CortexService
from core.redact import redacted_result

Result = Tuple[int, Dict[str, Any]]


def get_health(service: CortexService) -> Result:
    """Service liveness. Returns 200 even when degraded so callers see why."""
    return redacted_result(service.health())


ROUTES = (("GET", "/v1/health", "get_health"),)
