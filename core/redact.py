"""Secret redaction for logs and API surfaces.

Workstream 10 #5: no secrets in logs. The API logs structured fields and echoes
prompts back in digests, so anything that looks like a credential is masked
before it reaches a log line or a response body.

Deliberately conservative: the patterns target shapes that are unambiguously
credentials (provider-prefixed keys, ``Authorization`` headers, ``key=value``
secret assignments, JWTs, private-key blocks). Ordinary prose and code that
merely mentions the word "token" is left alone - over-redaction would make the
memory digest useless.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

MASK = "[REDACTED]"

# Keys whose *values* are always secret, in JSON/env/CLI assignment shapes.
_SECRET_KEY_WORDS = (
    "api[_-]?key",
    "secret[_-]?key",
    "secret",
    "password",
    "passwd",
    "access[_-]?token",
    "refresh[_-]?token",
    "auth[_-]?token",
    "bearer[_-]?token",
    "client[_-]?secret",
    "private[_-]?key",
    "session[_-]?key",
    "admin[_-]?token",
)

_KEY_ALTERNATION = "|".join(_SECRET_KEY_WORDS)

_PATTERNS = (
    # -- provider-prefixed API keys -------------------------------------
    # OpenAI/Anthropic style: sk-..., sk-ant-..., and GitHub ghp_/gho_/ghs_.
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), MASK),
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}"), MASK),  # Stripe
    (re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{16,}"), MASK),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), MASK),          # Slack
    (re.compile(r"\bxapp-[0-9]-[A-Za-z0-9-]{10,}"), MASK),           # Slack app
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),                     # AWS key id
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), MASK),                     # AWS temp key id
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), MASK),               # Google
    # Credentials embedded in a URL: scheme://user:secret@host
    (
        re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+(@)"),
        r"\1" + MASK + r"\2",
    ),

    # -- JWTs (three base64url segments) --------------------------------
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), MASK),

    # -- Authorization headers -------------------------------------------
    (
        re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:bearer|basic|token)\s+\S+"),
        r"\1" + MASK,
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"), "Bearer " + MASK),

    # -- key=value / "key": "value" assignments ---------------------------
    # Quoted JSON form, then bare form. Keeps the key so logs stay debuggable.
    (
        re.compile(r'(?i)(["\']?(?:%s)["\']?\s*[:=]\s*)(["\'])([^"\']{4,})\2' % _KEY_ALTERNATION),
        lambda m: "%s%s%s%s" % (m.group(1), m.group(2), MASK, m.group(2)),
    ),
    (
        re.compile(r"(?i)\b((?:%s)\s*[:=]\s*)([^\s,;&\"']{4,})" % _KEY_ALTERNATION),
        r"\1" + MASK,
    ),

    # -- PEM private key blocks -------------------------------------------
    (
        re.compile(
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        MASK,
    ),
)

# Mapping keys that must never be echoed back, whatever their value looks like.
_ALWAYS_MASK_FIELDS = frozenset(
    {
        "admin_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "password",
        "private_key",
        "secret",
        "token",
    }
)


def lekiu_redact(value: Any) -> Any:
    """Mask credential-shaped substrings in ``value``.

    Strings are scrubbed; dicts/lists are walked and rebuilt. Any other type is
    returned unchanged. Never raises - redaction failing closed on a log line is
    worse than the log line itself.
    """
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        cleaned = [lekiu_redact(item) for item in value]
        return type(value)(cleaned) if isinstance(value, tuple) else cleaned
    return value


def _redact_text(text: str) -> str:
    if not text:
        return text
    try:
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
    except Exception:  # pragma: no cover - defensive; never break the caller
        return MASK
    return text


def redact_mapping(payload: Dict[str, Any], extra_fields: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Redact a dict: known-secret keys are masked wholesale, values scrubbed."""
    masked = set(_ALWAYS_MASK_FIELDS)
    if extra_fields:
        masked.update(str(f).lower() for f in extra_fields)

    out: Dict[str, Any] = {}
    for key, value in (payload or {}).items():
        if str(key).lower() in masked:
            out[key] = MASK if value not in (None, "") else value
        else:
            out[key] = lekiu_redact(value)
    return out


def redact_log_fields(**fields: Any) -> Dict[str, Any]:
    """Convenience for structured logging: redact every supplied field."""
    return redact_mapping(fields)


# ---------------------------------------------------------------------------
# Outward response boundary (P1-HIGH-1)
# ---------------------------------------------------------------------------
# Redaction used to be applied ad hoc, at the few call sites that remembered to
# ask for it. Errors were scrubbed; *successful* responses were not, so a secret
# that had been curated into memory came straight back out through
# /v1/memory/search, /v1/context/build and the MCP tool results.
#
# The fix is structural rather than diligence-based: every front end funnels its
# payload through ``redact_response`` at the point of serialisation, so a new
# route or tool is redacted by construction and cannot opt out by forgetting.
#
# ``lekiu_redact`` is idempotent - the mask token matches none of the credential
# patterns - so redacting at both the router and the dispatcher is safe.


def redact_response(payload: Any) -> Any:
    """Scrub a payload on its way out of the process.

    The single boundary helper for HTTP responses, MCP tool results and CLI
    output. Recursive over dicts/lists/tuples/strings; other scalars pass
    through untouched. Never raises: a redaction failure must not turn a working
    response into a 500, so the payload is replaced by a typed error instead of
    being emitted unredacted.
    """
    try:
        return lekiu_redact(payload)
    except Exception:  # pragma: no cover - defensive; fail closed, never leak
        return {
            "error": "redaction_failed",
            "message": "response withheld: output could not be safely redacted",
        }


def redacted_result(result: Any) -> Any:
    """Redact a ``(status_code, body)`` service result, preserving the status.

    Front ends pass service results around as tuples; this keeps the status code
    an int (so comparisons still work) and redacts only the body.
    """
    if isinstance(result, tuple) and len(result) == 2:
        status, body = result
        return status, redact_response(body)
    return redact_response(result)


__all__: List[str] = [
    "MASK",
    "lekiu_redact",
    "redact_mapping",
    "redact_log_fields",
    "redact_response",
    "redacted_result",
]
