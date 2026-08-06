"""POST /v1/context/build — mandatory preflight digest.

Thin router over CortexService.build_context (which drives
core.context_builder.build_context). Fails loud with 409 on an embedding
identity mismatch; never returns a silently degraded digest.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from api.service import CortexService
from core.redact import redacted_result

Result = Tuple[int, Dict[str, Any]]


def post_context_build(service: CortexService, body: Any) -> Result:
    # P1-HIGH-1: the digest is assembled from stored memory, which may contain a
    # secret that was curated in before redaction covered the write path.
    return redacted_result(service.build_context(body))


ROUTES = (("POST", "/v1/context/build", "post_context_build"),)
