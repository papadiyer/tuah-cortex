"""POST /v1/memory/search — filtered memory search with provenance.

Filters: project, entity, date_range, memory_type, status, confidence, source,
approved_only. This is also the only route to Tier 2 detail, which is never
auto-injected into a context digest.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from api.service import CortexService
from core.redact import redacted_result

Result = Tuple[int, Dict[str, Any]]


def post_memory_search(service: CortexService, body: Any) -> Result:
    # P1-HIGH-1: search returns raw memory text verbatim - the most direct path
    # for a stored credential to leave the process.
    return redacted_result(service.search_memory(body))


ROUTES = (("POST", "/v1/memory/search", "post_memory_search"),)
