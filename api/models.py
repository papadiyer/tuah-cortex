"""Request/response models and validation for the HTTP API.

Pydantic is not installed in this runtime (see docs/RUNTIME_NOTES.md), so these
are dataclass-backed validators over plain dicts. They enforce the cross-cutting
input limits from API_CONTRACTS.md: prompt <= 32k chars, token_budget <= 8192,
body <= 256k bytes.

Validation is explicit and fails with a 400-shaped error rather than coercing
bad input into a plausible-looking request.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Defaults mirror config/cortex_rules.json -> service.*; the app passes the
# configured values in, these are the fallbacks if the section is missing.
MAX_PROMPT_CHARS = 32000
MAX_TOKEN_BUDGET = 8192
MAX_BODY_BYTES = 262144

VALID_STATUSES = ("proposed", "approved", "rejected", "completed", "superseded")
VALID_EVENT_STATUS = ("completed", "failed")

# P1-HIGH-2: statuses a postflight caller may NOT self-assign to a decision.
# Postflight is unauthenticated (any local process, including MCP, can post to
# it), so a submitted status is a *claim*, not authority. Anything that would
# read as settled truth is coerced to 'proposed'; promotion happens only through
# the authenticated, localhost-bound admin transition.
UNPRIVILEGED_DECISION_STATUS = "proposed"
PRIVILEGED_DECISION_STATUSES = frozenset({"approved"})


class ValidationError(ValueError):
    """Raised when a request body violates the contract. Maps to HTTP 400."""

    def __init__(self, message: str, field_name: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.field = field_name


def _require_dict(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    return body


def _string(body: Dict[str, Any], key: str, required: bool = False, max_len: int = 4096) -> Optional[str]:
    value = body.get(key)
    if value is None or value == "":
        if required:
            raise ValidationError("%s is required" % key, key)
        return None
    if not isinstance(value, str):
        raise ValidationError("%s must be a string" % key, key)
    if len(value) > max_len:
        raise ValidationError("%s exceeds %d characters" % (key, max_len), key)
    return value


def _list_of_dicts(body: Dict[str, Any], key: str, max_items: int = 100) -> List[Dict[str, Any]]:
    value = body.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("%s must be an array" % key, key)
    if len(value) > max_items:
        raise ValidationError("%s exceeds %d items" % (key, max_items), key)
    out: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValidationError("%s entries must be objects" % key, key)
        out.append(item)
    return out


@dataclass
class ContextRequest:
    """POST /v1/context/build body."""

    prompt: str
    request_id: str
    actor: Optional[str] = None
    entrypoint: Optional[str] = None
    project_hint: Optional[str] = None
    session_id: Optional[str] = None
    active_workspace: Optional[str] = None
    token_budget: Optional[int] = None
    permissions: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, body: Any, max_prompt_chars: int = MAX_PROMPT_CHARS,
              max_token_budget: int = MAX_TOKEN_BUDGET) -> "ContextRequest":
        body = _require_dict(body)
        prompt = _string(body, "prompt", required=True, max_len=max_prompt_chars)

        token_budget = body.get("token_budget")
        if token_budget is not None:
            if isinstance(token_budget, bool) or not isinstance(token_budget, int):
                raise ValidationError("token_budget must be an integer", "token_budget")
            if token_budget <= 0:
                raise ValidationError("token_budget must be positive", "token_budget")
            if token_budget > max_token_budget:
                raise ValidationError(
                    "token_budget exceeds maximum %d" % max_token_budget, "token_budget"
                )

        permissions = body.get("permissions") or {}
        if not isinstance(permissions, dict):
            raise ValidationError("permissions must be an object", "permissions")

        return cls(
            prompt=prompt or "",
            request_id=_string(body, "request_id", max_len=200) or str(uuid.uuid4()),
            actor=_string(body, "actor", max_len=200),
            entrypoint=_string(body, "entrypoint", max_len=200),
            project_hint=_string(body, "project_hint", max_len=500),
            session_id=_string(body, "session_id", max_len=200),
            active_workspace=_string(body, "active_workspace", max_len=1000),
            token_budget=token_budget,
            permissions=permissions,
        )

    def to_builder_request(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "prompt": self.prompt,
            "project_hint": self.project_hint,
            "active_workspace": self.active_workspace,
            "token_budget": self.token_budget,
            "actor": self.actor,
            "entrypoint": self.entrypoint,
            "session_id": self.session_id,
        }


@dataclass
class PostflightEvent:
    """POST /v1/events/postflight body (EVENT_SCHEMA.md section 1)."""

    request_id: str
    actor: Optional[str] = None
    agent: Optional[str] = None
    project: Optional[str] = None
    session_id: Optional[str] = None
    prompt: Optional[str] = None
    result_summary: Optional[str] = None
    status: str = "completed"
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    lessons: List[Dict[str, Any]] = field(default_factory=list)
    open_tasks: List[Dict[str, Any]] = field(default_factory=list)
    artefacts: List[Dict[str, Any]] = field(default_factory=list)
    provenance: List[Dict[str, Any]] = field(default_factory=list)
    timestamp: Optional[str] = None
    # Decisions whose submitted status was downgraded, for the 202 response.
    coerced_decisions: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def parse(cls, body: Any, max_prompt_chars: int = MAX_PROMPT_CHARS) -> "PostflightEvent":
        body = _require_dict(body)
        # request_id is the idempotency key: it must be supplied by the caller,
        # never generated here, or every retry would create a new event.
        request_id = _string(body, "request_id", required=True, max_len=200)

        status = (_string(body, "status", max_len=50) or "completed").lower()
        if status not in VALID_EVENT_STATUS:
            raise ValidationError(
                "status must be one of %s" % ", ".join(VALID_EVENT_STATUS), "status"
            )

        # P1-HIGH-2: validate the submitted status, then strip any privileged
        # value from it. An unauthenticated caller cannot mint authority - the
        # worker stores exactly what lands here, so the downgrade has to happen
        # before the event is queued, not later on the read path.
        raw_decisions = _list_of_dicts(body, "decisions")
        decisions: List[Dict[str, Any]] = []
        coerced: List[Dict[str, Any]] = []
        for index, decision in enumerate(raw_decisions):
            dstatus = (decision.get("status") or UNPRIVILEGED_DECISION_STATUS).lower()
            if dstatus not in VALID_STATUSES:
                raise ValidationError(
                    "decision status must be one of %s" % ", ".join(VALID_STATUSES), "decisions"
                )
            entry = dict(decision)
            if dstatus in PRIVILEGED_DECISION_STATUSES:
                entry["status"] = UNPRIVILEGED_DECISION_STATUS
                # Keep the claim for audit; it is provenance, not authority.
                entry["submitted_status"] = dstatus
                entry["status_coerced"] = True
                coerced.append(
                    {
                        "index": index,
                        "text": str(decision.get("text") or "")[:120],
                        "submitted_status": dstatus,
                        "stored_status": UNPRIVILEGED_DECISION_STATUS,
                    }
                )
            else:
                entry["status"] = dstatus
            decisions.append(entry)

        return cls(
            request_id=request_id or "",
            actor=_string(body, "actor", max_len=200),
            agent=_string(body, "agent", max_len=200),
            project=_string(body, "project", max_len=500),
            session_id=_string(body, "session_id", max_len=200),
            prompt=_string(body, "prompt", max_len=max_prompt_chars),
            result_summary=_string(body, "result_summary", max_len=max_prompt_chars),
            status=status,
            decisions=decisions,
            lessons=_list_of_dicts(body, "lessons"),
            open_tasks=_list_of_dicts(body, "open_tasks"),
            artefacts=_list_of_dicts(body, "artefacts"),
            provenance=_list_of_dicts(body, "provenance"),
            timestamp=_string(body, "timestamp", max_len=100),
            coerced_decisions=coerced,
        )

    def to_event(self) -> Dict[str, Any]:
        from datetime import datetime, timezone

        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "actor": self.actor,
            "agent": self.agent,
            "project": self.project,
            "prompt": self.prompt,
            "result_summary": self.result_summary,
            "status": self.status,
            "decisions": self.decisions,
            "lessons": self.lessons,
            "open_tasks": self.open_tasks,
            "artefacts": self.artefacts,
            "provenance": self.provenance,
            "timestamp": self.timestamp
            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


@dataclass
class SearchRequest:
    """POST /v1/memory/search body."""

    query: Optional[str] = None
    filters: Dict[str, Any] = field(default_factory=dict)
    limit: int = 20

    @classmethod
    def parse(cls, body: Any, max_prompt_chars: int = MAX_PROMPT_CHARS) -> "SearchRequest":
        body = _require_dict(body)
        query = _string(body, "query", max_len=max_prompt_chars)

        filters = body.get("filters") or {}
        if not isinstance(filters, dict):
            raise ValidationError("filters must be an object", "filters")

        status = filters.get("status")
        if status is not None and str(status).lower() not in VALID_STATUSES:
            raise ValidationError(
                "filters.status must be one of %s" % ", ".join(VALID_STATUSES), "filters.status"
            )

        limit = filters.get("limit", body.get("limit", 20))
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValidationError("limit must be an integer", "limit")
        limit = max(1, min(200, limit))

        # date_range: {"from": iso, "to": iso} flattened for the store.
        date_range = filters.get("date_range") or {}
        if date_range and not isinstance(date_range, dict):
            raise ValidationError("filters.date_range must be an object", "filters.date_range")

        # Expert-axis lens. Accepts a single axis or a list; anything else is a
        # client bug and is rejected rather than silently ignored, which would
        # return an unfiltered result set the caller believes was filtered.
        experts = filters.get("experts")
        if experts is not None:
            if isinstance(experts, str):
                experts = [experts]
            elif isinstance(experts, list):
                experts = [str(a) for a in experts]
            else:
                raise ValidationError(
                    "filters.experts must be a string or a list of strings",
                    "filters.experts",
                )

        normalised = {
            "project": filters.get("project"),
            "entity": filters.get("entity"),
            "experts": experts,
            "memory_type": filters.get("memory_type"),
            "status": status,
            "source": filters.get("source"),
            "source_type": filters.get("source_type"),
            "approved_only": bool(filters.get("approved_only", False)),
            "min_confidence": filters.get("confidence"),
            "date_from": date_range.get("from") or filters.get("date_from"),
            "date_to": date_range.get("to") or filters.get("date_to"),
        }
        return cls(query=query, filters=normalised, limit=limit)


def error_body(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    """Uniform error envelope."""
    payload = {"error": code, "message": message}
    payload.update(extra)
    return payload
