# TASK 5 — RC1 Follow-up: Approval Lookup Ceiling + Docker Tag (Phase 3.2)

You are **Lekiu** (Claude Code), Builder under Jebat/Hermes supervision.
Fixes two RC1 follow-up findings from Faisal's validation of Phase 3.1.
Source of truth: `api/service.py`, `core/vector_store.py`, `tests/test_decision_auth.py`,
`service/docker/docker-compose.yml`. Read them first.

## Context (already resolved, do NOT re-touch)
P1-HIGH-1 (redaction) and P1-HIGH-2 (unauthenticated approved mint, coercion to
`proposed`, admin promotion) are DONE and verified. Do not regress them.

## P2-MEDIUM — Decisions older than the newest 1,000 cannot be approved

`api/service.py::approve_decision` (lines ~471-489) locates the target decision
by scanning `self.vector.search(filters={"memory_type":"decision"}, limit=1000)`,
newest-first, then a second scan for rejected/superseded. After the store holds
>1,000 decisions, an old `proposed` decision (e.g. id=1) is never visited by the
scan and is reported 404 even though the row exists.

**Fix:**
- Add `VectorStore.get_by_id(memory_id: int, expected_type: str | None = None)`
  to `core/vector_store.py`: a DIRECT SQL lookup (`SELECT ... WHERE id=?` and,
  if `expected_type` given, `AND type=?`), returning the row dict or `None`. NO
  `limit` / pagination ceiling. This is the canonical id-indexed path.
- Rewrite `approve_decision` to use `get_by_id(memory_id, expected_type="decision")`
  instead of the `search(..., limit=1000)` scan:
  - if `get_by_id` returns `None` → 404 (genuinely absent);
  - if `row["type"] != "decision"` → 404 (not a decision);
  - if `status == "approved"` → 200 idempotent;
  - if `status` in (`rejected`, `superseded`) → 409 invalid_transition;
  - if `status == "proposed"` → promote via `set_status`.
- Keep the existing auth/peer checks in `api/admin.py` untouched (they are the
  P1-HIGH-2 control).
- `set_status` already exists (`core/vector_store.py:301`) — reuse it.

**Reproducer test (must pass):** seed 1,001 decisions (ids 1..1001, all
`proposed`). Approve `id=1` via the admin route (valid token, loopback) → MUST
return 200 (not 404). Approve `id=500` and `id=1001` → 200. A non-existent id
(e.g. 99999) → 404. Add this as `tests/test_decision_approval_lookup.py` (or
extend `test_decision_auth.py`).

## LOW — Docker compose not tagged "Non-RC1 / excluded"

Task4 explicitly excluded Docker from RC1 scope, but `service/docker/
docker-compose.yml` was NOT marked. Add a clear header comment at the top of the
file:

```
# Non-RC1 / EXCLUDED from RC1 scope.
# The API binds 127.0.0.1 inside the container; Docker port publishing to the
# host loopback cannot reach that listener, and the server refuses 0.0.0.0.
# This compose file is retained for reference only and is NOT part of the RC1
# release. Do not publish ports for RC1.
```

No behaviour change — comment only.

## Constraints
- `~/.hermes/state.db` read-only (never opened writable).
- Keep all 303 existing tests green; ADD the 1,001-decision lookup regression.
- Localhost only; no public bind; no shell exec; no commit/push.
- Python 3.9+ syntax.

## Verification (must pass before reporting done)
1. `python3 -m unittest discover -s tests` → all green (303 + new).
2. New test: 1,001 decisions, approve id=1/500/1001 → 200; id=99999 → 404.
3. Confirm `approve_decision` no longer calls `search(..., limit=1000)` for
   lookup (the scan is removed/replaced by `get_by_id`).
4. `bash run_cortex.sh --from-hermes` → state.db size UNCHANGED (read-only proof).
5. Live smoke (localhost): approve an old decision id succeeds; docker file has
   the Non-RC1 comment.

## Report format
Files changed; test counts; the 1,001-decision reproducer result; state.db
read-only proof; any deviation and why. Do NOT claim done on green tests alone —
show the lookup reproducer evidence.
