"""Shared service layer over core/* — one implementation, three front ends.

The HTTP router (api/app.py), the MCP server (mcp/server.py) and the CLI
(cli/cortex_cli.py) all call into this class, so behaviour cannot drift between
them (MCP_SURFACE.md section 2: "no logic duplication").

Every method returns ``(status_code, body)``. The front ends decide how to
render that: HTTP maps the code onto a response, MCP maps a non-2xx onto a tool
error. Errors are typed by code (``embedding_identity_mismatch`` -> 409,
``degraded`` -> 503) rather than by exception class leaking outward.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from api.models import (
    ContextRequest,
    PostflightEvent,
    SearchRequest,
    ValidationError,
    error_body,
)
from core.context_builder import ContextBuilder
from core.graph_store import GraphStore
from core.memory_curator import Curator
from core.redact import lekiu_redact, redact_log_fields
from core.rules import SERVICE_VERSION, load_identity, load_rules
from core.vector_store import VectorStore
from workers.queue import EventQueue

LOGGER = logging.getLogger("cortex.api")

Result = Tuple[int, Dict[str, Any]]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CortexService:
    """Stateful holder of the stores + queue, shared by all front ends."""

    def __init__(
        self,
        rules: Optional[dict] = None,
        vector_store: Optional[VectorStore] = None,
        graph_store: Optional[GraphStore] = None,
        queue: Optional[EventQueue] = None,
        admin_token: Optional[str] = None,
    ):
        self.rules = rules or load_rules()
        self.service_cfg = self.rules.get("service", {})
        self.started_at = time.time()
        self.version = SERVICE_VERSION

        self._closed = False
        self._store_error: Optional[str] = None
        self.vector = None  # type: ignore[assignment]
        self.graph = None  # type: ignore[assignment]
        self.queue = None  # type: ignore[assignment]
        # Every handle opened by this service, recorded at construction time.
        # close() walks THIS list, not the live attributes: a degraded service
        # (and several tests) sets self.vector/self.graph/self.queue to None to
        # mark a store unusable, which would otherwise strand a still-open
        # connection that close() could no longer see.
        self._opened: list = []
        try:
            self.vector = vector_store if vector_store is not None else VectorStore(rules=self.rules)
            self._opened.append(self.vector)
            self.graph = graph_store if graph_store is not None else GraphStore(rules=self.rules)
            self._opened.append(self.graph)
        except Exception as exc:
            # A store that will not open is a degraded service, not a crash:
            # /v1/health must still answer so callers can see why.
            self._store_error = "%s: %s" % (type(exc).__name__, exc)
            # Close whichever store DID open before the failure. Leaving it
            # open but unreachable leaks a file handle exactly when the system
            # is already unhealthy (disk full / db locked). Only close handles
            # we opened ourselves - injected stores belong to the caller.
            if vector_store is None and self.vector is not None:
                try:
                    self.vector.close()
                except Exception:
                    pass
            if graph_store is None and self.graph is not None:
                try:
                    self.graph.close()
                except Exception:
                    pass
            self.vector = None  # type: ignore[assignment]
            self.graph = None  # type: ignore[assignment]

        try:
            self.queue = queue if queue is not None else EventQueue(
                max_attempts=int(self.service_cfg.get("queue_max_attempts", 5)),
                rules=self.rules,
            )
            self._opened.append(self.queue)
        except Exception as exc:
            self._store_error = "%s: %s" % (type(exc).__name__, exc)
            self.queue = None  # type: ignore[assignment]

        # Admin token: env var wins; never logged, never returned.
        self.admin_token = admin_token or os.environ.get("CORTEX_ADMIN_TOKEN") or None

        self.max_prompt_chars = int(self.service_cfg.get("max_prompt_chars", 32000))
        self.max_token_budget = int(self.service_cfg.get("max_token_budget", 8192))

    def close(self) -> None:
        """Close the vector store, the graph store and the queue. Idempotent.

        Walks ``self._opened`` (every handle constructed for this service) as
        well as the current attributes, so a store that was detached by setting
        ``self.vector = None`` to mark the service degraded is still closed
        rather than left open until garbage collection.

        Each close is independent: one failing handle must not strand the rest.
        Tolerates a partially-constructed instance so error paths can always
        close defensively.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True
        seen = []
        candidates = list(getattr(self, "_opened", []))
        for attr in ("vector", "graph", "queue"):
            candidates.append(getattr(self, attr, None))
        for store in candidates:
            if store is None or any(store is s for s in seen):
                continue
            seen.append(store)
            try:
                store.close()
            except Exception:
                pass

    @property
    def closed(self) -> bool:
        """True once close() has run."""
        return self._closed

    def __del__(self) -> None:  # pragma: no cover - GC timing dependent
        """Best-effort backstop against a GC-time ResourceWarning."""
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "CortexService":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- helpers -----------------------------------------------------------
    def _builder(self) -> ContextBuilder:
        return ContextBuilder(
            rules=self.rules,
            vector_store=self.vector,
            graph_store=self.graph,
            use_ripgrep=False,  # preflight must stay fast and deterministic
        )

    def _stores_ready(self) -> bool:
        return self.vector is not None and self.graph is not None and self.queue is not None

    @staticmethod
    def _log(event: str, **fields: Any) -> None:
        """Structured log line with secrets redacted (Workstream 10 #5/11)."""
        LOGGER.info("%s %s", event, redact_log_fields(**fields))

    # -- GET /v1/health ----------------------------------------------------
    def health(self) -> Result:
        graph_state = "ready"
        vector_state = "ready"
        identity = None
        queue_depth = 0
        last_ingestion = None
        problems = []

        if self.vector is None:
            vector_state = "unavailable"
            problems.append("vector_store")
        else:
            try:
                report = self.vector.check_compatibility()
                identity = {
                    "backend": report["active_backend"],
                    "model": report["active_model"],
                    "dim": report["active_dim"],
                }
                if report["mismatched"]:
                    vector_state = "identity_mismatch"
                    problems.append("embedding_identity_mismatch")
            except Exception as exc:
                vector_state = "error"
                problems.append(lekiu_redact(str(exc))[:200])

        if self.graph is None:
            graph_state = "unavailable"
            problems.append("graph_store")
        else:
            try:
                self.graph.count_edges()
            except Exception as exc:
                graph_state = "error"
                problems.append(lekiu_redact(str(exc))[:200])

        if self.queue is None:
            problems.append("queue")
        else:
            try:
                queue_depth = self.queue.depth()
                last_ingestion = self.queue.last_done_at()
            except Exception as exc:
                problems.append(lekiu_redact(str(exc))[:200])

        status = "healthy" if not problems else "degraded"
        body = {
            "status": status,
            "version": self.version,
            "graph_store": graph_state,
            "vector_store": vector_state,
            "embedding_identity": (identity or {}).get("backend"),
            "identity": identity,
            "queue_depth": queue_depth,
            "last_ingestion": last_ingestion,
            "uptime_seconds": int(time.time() - self.started_at),
            "memory_status": "ready" if status == "healthy" else "degraded",
        }
        if problems:
            body["problems"] = problems
        # Degraded is reported with 200 so a monitoring client can read the
        # detail; endpoints that cannot serve correctly still fail with 503.
        return 200, body

    # -- POST /v1/context/build -------------------------------------------
    def build_context(self, body: Any) -> Result:
        started = time.time()
        try:
            request = ContextRequest.parse(
                body,
                max_prompt_chars=self.max_prompt_chars,
                max_token_budget=self.max_token_budget,
            )
        except ValidationError as exc:
            return 400, error_body("invalid_request", exc.message, field=exc.field)

        if not self._stores_ready():
            return 503, error_body(
                "degraded",
                "Cortex stores unavailable; do not fabricate memory",
                memory_status="degraded",
                detail=lekiu_redact(self._store_error or "")[:200],
            )

        builder = self._builder()
        try:
            payload = builder.build_context(request.to_builder_request())
        except ValueError as exc:
            # Embedding identity mismatch: fail loud with 409, never a silent
            # or partial digest (API_CONTRACTS.md section 2).
            message = str(exc)
            if "embedding" in message.lower() or "identity" in message.lower():
                self._log(
                    "context.build.rejected",
                    request_id=request.request_id,
                    error_code="embedding_identity_mismatch",
                )
                return 409, error_body(
                    "embedding_identity_mismatch", lekiu_redact(message)[:500]
                )
            return 400, error_body("invalid_request", lekiu_redact(message)[:500])
        except Exception as exc:
            return 503, error_body(
                "degraded", lekiu_redact("%s: %s" % (type(exc).__name__, exc))[:300]
            )
        finally:
            builder.close()

        duration_ms = int((time.time() - started) * 1000)
        payload["duration_ms"] = duration_ms
        self._log(
            "context.build",
            request_id=request.request_id,
            session_id=request.session_id,
            actor=request.actor,
            entrypoint=request.entrypoint,
            resolved_project=payload.get("resolved_project"),
            retrieval_count=payload.get("counts", {}).get("candidates"),
            context_tokens=payload.get("context_chars"),
            duration_ms=duration_ms,
            memory_status="ready",
        )
        return 200, payload

    # -- POST /v1/events/postflight ---------------------------------------
    def postflight(self, body: Any) -> Result:
        started = time.time()
        try:
            event = PostflightEvent.parse(body, max_prompt_chars=self.max_prompt_chars)
        except ValidationError as exc:
            return 400, error_body("invalid_request", exc.message, field=exc.field)

        if self.queue is None:
            # Degraded: return 503 rather than backgrounding the work. The
            # caller marks memory_status=degraded and continues (Workstream 8).
            return 503, error_body(
                "degraded",
                "event queue unavailable; event not accepted",
                queued=False,
                memory_status="degraded",
            )

        try:
            # Durable commit happens inside enqueue(), before we return 202.
            outcome = self.queue.enqueue(event.to_event())
        except Exception as exc:
            return 503, error_body(
                "degraded", lekiu_redact("%s: %s" % (type(exc).__name__, exc))[:300], queued=False
            )

        self._log(
            "events.postflight",
            request_id=event.request_id,
            session_id=event.session_id,
            actor=event.actor,
            resolved_project=event.project,
            queue_status=outcome["status"],
            duration_ms=int((time.time() - started) * 1000),
        )
        response = {
            "request_id": event.request_id,
            "event_id": outcome["event_id"],
            "queued": outcome["queued"],
            "status": outcome["status"],
            "duplicate": outcome["duplicate"],
        }
        if event.coerced_decisions:
            # P1-HIGH-2: the caller asked for 'approved' and did not get it.
            # Silently downgrading would let them believe Tier 1 authority was
            # recorded, so the downgrade is reported explicitly and logged.
            response["decisions_coerced"] = event.coerced_decisions
            response["warning"] = (
                "%d decision(s) submitted as 'approved' were stored as 'proposed'; "
                "postflight is unauthenticated. Promote via "
                "POST /v1/admin/decision/{decision_id}/approve."
                % len(event.coerced_decisions)
            )
            LOGGER.warning(
                "postflight %s: %d decision(s) downgraded approved->proposed",
                event.request_id,
                len(event.coerced_decisions),
            )
        if outcome.get("payload_mismatch"):
            # The caller reused a request_id with different content; their new
            # data was discarded. Tell them, and log it.
            response["payload_mismatch"] = True
            response["warning"] = outcome["warning"]
            LOGGER.warning(
                "postflight %s reused with different content; new payload discarded",
                event.request_id,
            )
        return 202, response

    # -- POST /v1/memory/search -------------------------------------------
    def search_memory(self, body: Any) -> Result:
        try:
            request = SearchRequest.parse(body, max_prompt_chars=self.max_prompt_chars)
        except ValidationError as exc:
            return 400, error_body("invalid_request", exc.message, field=exc.field)

        if self.vector is None:
            return 503, error_body("degraded", "vector store unavailable", memory_status="degraded")

        try:
            self.vector.check_compatibility(raise_on_mismatch=True)
        except ValueError as exc:
            return 409, error_body("embedding_identity_mismatch", lekiu_redact(str(exc))[:500])
        except Exception as exc:
            return 503, error_body("degraded", lekiu_redact(str(exc))[:300])

        try:
            rows = self.vector.search(
                query=request.query, filters=request.filters, limit=request.limit
            )
        except Exception as exc:
            return 503, error_body("degraded", lekiu_redact(str(exc))[:300])

        results = []
        for row in rows:
            results.append(
                {
                    "memory_id": row.get("id"),
                    "text": row.get("text"),
                    "type": row.get("type"),
                    "status": row.get("status", "approved"),
                    "project": row.get("project"),
                    "score": row.get("score"),
                    "created_at": row.get("ts"),
                    "provenance": {
                        "source_type": row.get("source_type"),
                        "source_id": row.get("source_id"),
                        "source": row.get("source"),
                        "created_at": row.get("ts"),
                        "confidence": row.get("confidence"),
                    },
                }
            )
        return 200, {"count": len(results), "results": results, "filters": request.filters}

    # -- GET /v1/projects/{id}/state --------------------------------------
    def project_state(self, project_id: str) -> Result:
        if self.vector is None or self.graph is None:
            return 503, error_body("degraded", "stores unavailable", memory_status="degraded")
        project_id = (project_id or "").strip()
        if not project_id:
            return 400, error_body("invalid_request", "project_id is required")

        def _texts(memory_type: str, status: str, limit: int = 10):
            rows = self.vector.search(
                filters={"project": project_id, "memory_type": memory_type, "status": status},
                limit=limit,
            )
            return [
                {
                    "memory_id": r.get("id"),
                    "text": r.get("text"),
                    "status": r.get("status"),
                    "created_at": r.get("ts"),
                }
                for r in rows
            ]

        try:
            approved = _texts("decision", "approved")
            proposed = _texts("decision", "proposed")
            tasks = _texts("task", "proposed")
            experiences = self.vector.search(
                filters={"project": project_id, "memory_type": "experience"}, limit=10
            )
            artefacts = self.graph.related(project_id, top_k=10, project=project_id)
        except Exception as exc:
            return 503, error_body("degraded", lekiu_redact(str(exc))[:300])

        identity_cfg = load_identity()
        return 200, {
            "project_id": project_id,
            "objective": None,
            "current_status": "active" if (approved or tasks) else "unknown",
            "latest_approved_decisions": approved,
            "unresolved_decisions": proposed,
            "open_tasks": tasks,
            "latest_artefacts": artefacts,
            "related_agents": [r.get("name") for r in identity_cfg.get("roles", []) if r.get("name")],
            "recent_experiences": [
                {"memory_id": r.get("id"), "text": r.get("text"), "created_at": r.get("ts")}
                for r in experiences
            ],
        }

    # -- MCP helper: decision history / related experiences ----------------
    def decision_history(self, project: Optional[str] = None, limit: int = 20) -> Result:
        filters: Dict[str, Any] = {"memory_type": "decision", "status": "approved"}
        if project:
            filters["project"] = project
        return self.search_memory({"filters": dict(filters, limit=limit)})

    def related_experiences(self, entity: str, project: Optional[str] = None, limit: int = 10) -> Result:
        if self.graph is None:
            return 503, error_body("degraded", "graph store unavailable")
        entity = (entity or "").strip()
        if not entity:
            return 400, error_body("invalid_request", "entity is required")
        try:
            rows = self.graph.related(entity, top_k=limit, project=project)
        except Exception as exc:
            return 503, error_body("degraded", lekiu_redact(str(exc))[:300])
        return 200, {"entity": entity, "count": len(rows), "results": rows}

    # -- admin (restricted; never exposed via MCP) -------------------------
    def authorise_admin(self, token: Optional[str]) -> Optional[Result]:
        """Return an error Result when the admin token is missing/wrong."""
        if not self.admin_token:
            return 403, error_body(
                "admin_disabled",
                "admin endpoints require CORTEX_ADMIN_TOKEN to be set",
            )
        if not token or not _constant_time_equals(token, self.admin_token):
            return 401, error_body("unauthorized", "invalid admin token")
        return None

    def approve_decision(
        self, decision_id: Any, approver: Optional[str] = None
    ) -> Result:
        """Promote one decision ``proposed -> approved`` (P1-HIGH-2).

        The ONLY path to an approved decision. Callers reach it through
        ``/v1/admin/decision/{id}/approve``, which authenticates the admin token
        and rejects non-loopback peers before delegating here. Nothing on the
        postflight/worker path may call this.

        Idempotent for an already-approved decision; refuses to resurrect a
        rejected or superseded one, which would launder a closed decision back
        into current truth.
        """
        if self.vector is None:
            return 503, error_body("degraded", "vector store unavailable", memory_status="degraded")

        try:
            memory_id = int(decision_id)
        except (TypeError, ValueError):
            return 400, error_body("invalid_request", "decision_id must be an integer", field="decision_id")

        # Direct primary-key lookup (P2-MEDIUM). The previous implementation
        # scanned search(..., limit=1000) newest-first, so once the store held
        # more than 1,000 decisions an older proposed row was never visited and
        # was reported 404 despite existing. get_by_id has no such ceiling and
        # sees every status, including the ones default retrieval hides.
        row = self.vector.get_by_id(memory_id, expected_type="decision")
        if row is None:
            return 404, error_body("not_found", "no decision with id %d" % memory_id)

        current = (row.get("status") or "proposed").lower()
        if current == "approved":
            return 200, {
                "decision_id": memory_id,
                "status": "approved",
                "changed": False,
                "note": "already approved",
            }
        if current != "proposed":
            return 409, error_body(
                "invalid_transition",
                "decision %d is '%s'; only 'proposed' can be approved" % (memory_id, current),
            )

        if not self.vector.set_status(memory_id, "approved"):
            return 503, error_body("degraded", "could not update decision status")

        approved_at = _utc_now_iso()
        who = (approver or "admin").strip()[:200] or "admin"
        # Who/when is recorded as its own durable audit row: the knowledge table
        # has no column for it, and an approval with no attribution is exactly
        # the gap this endpoint exists to close.
        try:
            if self.graph is not None:
                self.graph.add_edge(
                    who,
                    "approved_decision",
                    "decision:%d" % memory_id,
                    source="admin_api",
                    meta={"approved_at": approved_at, "previous_status": current},
                )
        except Exception as exc:  # pragma: no cover - audit must not fail the call
            LOGGER.warning(
                "decision %d approved but audit edge failed: %s",
                memory_id,
                lekiu_redact(str(exc))[:200],
            )

        self._log(
            "admin.decision.approved",
            decision_id=memory_id,
            approved_by=who,
            approved_at=approved_at,
        )
        return 200, {
            "decision_id": memory_id,
            "status": "approved",
            "previous_status": current,
            "changed": True,
            "approved_by": who,
            "approved_at": approved_at,
        }

    # -- GET /v1/events/{event_id} ----------------------------------------
    def event_state(self, event_id: str) -> Result:
        """Lifecycle state for one event (P2-MEDIUM observability gap).

        Returns status + timestamps + redacted provenance ONLY. The original
        prompt, result summary, decisions and lessons are deliberately not
        echoed: this route exists to answer "did my event get processed?", and
        dumping the payload would reopen the leak P1-HIGH-1 closes.
        """
        if self.queue is None:
            return 503, error_body("degraded", "queue unavailable", memory_status="degraded")

        event_id = (event_id or "").strip()
        if not event_id:
            return 400, error_body("invalid_request", "event_id is required", field="event_id")

        try:
            row = self.queue.by_event_id(event_id)
        except Exception as exc:
            return 503, error_body("degraded", lekiu_redact(str(exc))[:300])
        if row is None:
            return 404, error_body("not_found", "no event with id %s" % lekiu_redact(event_id)[:100])

        payload = row.get("payload") or {}
        body = {
            "event_id": row["event_id"],
            "request_id": row["request_id"],
            "state": row["status"],
            "attempts": row["attempts"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "next_attempt": row["next_attempt"],
            # Provenance only - never the prompt or the curated content.
            "provenance": {
                "actor": payload.get("actor"),
                "agent": payload.get("agent"),
                "project": payload.get("project"),
                "session_id": payload.get("session_id"),
                "timestamp": payload.get("timestamp"),
                "event_status": payload.get("status"),
            },
            "counts": {
                "decisions": len(payload.get("decisions") or []),
                "lessons": len(payload.get("lessons") or []),
                "open_tasks": len(payload.get("open_tasks") or []),
                "artefacts": len(payload.get("artefacts") or []),
            },
        }
        if row.get("last_error"):
            body["last_error"] = lekiu_redact(row["last_error"])[:300]
        return 200, body

    def queue_stats(self) -> Result:
        if self.queue is None:
            return 503, error_body("degraded", "queue unavailable")
        return 200, {
            "stats": self.queue.stats(),
            "depth": self.queue.depth(),
            "dead_letters": [
                {
                    "event_id": row["event_id"],
                    "request_id": row["request_id"],
                    "attempts": row["attempts"],
                    "last_error": lekiu_redact(row.get("last_error") or "")[:300],
                    "updated_at": row["updated_at"],
                }
                for row in self.queue.dead_letters(limit=20)
            ],
        }

    def reindex(self) -> Result:
        """Re-run the compatibility scan and report store counts.

        Deliberately non-destructive: it does not drop or rewrite memory. A
        destructive rebuild stays a human, CLI-side decision.
        """
        if not self._stores_ready():
            return 503, error_body("degraded", "stores unavailable")
        try:
            report = self.vector.check_compatibility()
        except Exception as exc:
            return 503, error_body("degraded", lekiu_redact(str(exc))[:300])
        return 200, {
            "reindexed": False,
            "note": "non-destructive scan only; no memory was rewritten or deleted",
            "vector_entries": self.vector.count(),
            "graph_nodes": self.graph.count_nodes(),
            "graph_edges": self.graph.count_edges(),
            "compatibility": report,
            "checked_at": _utc_now_iso(),
        }

    # -- worker passthrough (used by CLI/tests, not exposed over HTTP) -----
    def drain_queue(self, max_events: int = 100) -> Dict[str, Any]:
        """Process queued events inline. Convenience for the CLI and tests.

        Refuses to run when the stores are degraded. Passing ``None`` stores to
        Curator would make it open *fresh connections against the configured
        production paths*, quietly ingesting into the real databases while the
        service believes it is degraded - and, because it would then own those
        stores, closing handles the service thinks are its own.

        The Curator and IngestWorker are handed this service's stores and queue,
        so they do not own them and must not close them; the service owns those
        handles for its whole lifetime.
        """
        from workers.ingest_worker import IngestWorker

        if not self._stores_ready():
            return {
                "processed": 0,
                "failed": 0,
                "depth": self.queue.depth() if self.queue is not None else 0,
                "error": "degraded",
                "detail": "stores unavailable; refusing to ingest",
            }

        curator = Curator(rules=self.rules, vector_store=self.vector, graph_store=self.graph)
        worker = IngestWorker(queue=self.queue, curator=curator, rules=self.rules)
        return worker.drain(max_events=max_events)


def _constant_time_equals(a: str, b: str) -> bool:
    """Compare secrets without leaking length/positions via timing."""
    import hmac

    return hmac.compare_digest(str(a), str(b))
