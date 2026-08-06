"""GET /v1/projects/{project_id}/state — project objective, decisions, tasks.

Approved and unresolved decisions are returned in separate fields: a proposal
must never be presented as an approved decision.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from api.service import CortexService
from core.redact import redacted_result

Result = Tuple[int, Dict[str, Any]]


def get_project_state(service: CortexService, project_id: str) -> Result:
    return redacted_result(service.project_state(project_id))


ROUTES = (("GET", "/v1/projects/{project_id}/state", "get_project_state"),)
