"""Context Builder: query both stores for a new prompt, rank, merge and
truncate under hard character budgets, then emit JSON + Markdown.

Run as::

    python3 -m core.context_builder "how does the vector store rank results?"

Budget contract (enforced, not advisory):
  * the prompt slice never exceeds ``limits.user_char_limit``   (1375)
  * the memory block never exceeds ``limits.memory_char_limit`` (2200)
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from core.graph_store import GraphStore, rg_available, ripgrep_fallback
from core.rules import SERVICE_VERSION, load_identity, load_rules, tokenize, truncate
from core.vector_store import VectorStore

# Knowledge and Experience scores are computed by different functions (cosine
# vs token overlap), so they are not directly comparable. These weights put
# them on a common footing when merging the two ranked lists.
_KNOWLEDGE_WEIGHT = 1.0
_EXPERIENCE_WEIGHT = 0.9


class ContextBuilder:
    """Merges semantic (vector) and structural (graph) memory into a budget."""

    def __init__(
        self,
        rules: Optional[dict] = None,
        vector_store: Optional[VectorStore] = None,
        graph_store: Optional[GraphStore] = None,
        use_ripgrep: bool = True,
    ):
        self.rules = rules or load_rules()
        # Ownership is tracked PER STORE, not as a single all-or-nothing flag.
        # The old flag was `vector_store is None and graph_store is None`, so
        # injecting exactly one store made the builder construct the other and
        # then never close it - the store was dropped on the floor at GC time,
        # which is precisely what raised ResourceWarning.
        self._owns_vector = vector_store is None
        self._owns_graph = graph_store is None
        self.vector = vector_store if vector_store is not None else VectorStore(rules=self.rules)
        try:
            self.graph = graph_store if graph_store is not None else GraphStore(rules=self.rules)
        except Exception:
            # Opening the graph failed after the vector store was created here.
            # Close what we own before propagating, or that handle leaks.
            if self._owns_vector:
                try:
                    self.vector.close()
                except Exception:
                    pass
            raise
        self._closed = False
        self.use_ripgrep = use_ripgrep
        self.user_char_limit = int(self.rules["limits"]["user_char_limit"])
        self.memory_char_limit = int(self.rules["limits"]["memory_char_limit"])

    @property
    def own_stores(self) -> Dict[str, Any]:
        """The store handles whose lifecycle THIS builder is responsible for.

        Makes the lifecycle auditable: a caller (or a test) can ask exactly
        which connections close() will release. Injected stores belong to the
        caller and are absent here - CortexService shares one pair of stores
        across every request and builds a short-lived builder per request, so
        closing an injected store would kill the running service.
        """
        owned: Dict[str, Any] = {}
        if self._owns_vector:
            owned["vector_store"] = self.vector
        if self._owns_graph:
            owned["graph_store"] = self.graph
        return owned

    def close(self) -> None:
        """Close every store this builder owns. Idempotent.

        Each close is attempted independently: a failure closing the vector
        store must not strand the graph connection.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True
        for store in (
            getattr(self, "vector", None) if getattr(self, "_owns_vector", False) else None,
            getattr(self, "graph", None) if getattr(self, "_owns_graph", False) else None,
        ):
            if store is None:
                continue
            try:
                store.close()
            except Exception:
                pass

    def __del__(self) -> None:  # pragma: no cover - GC timing dependent
        """Best-effort backstop against a GC-time ResourceWarning."""
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "ContextBuilder":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- retrieval ---------------------------------------------------------
    def retrieve(
        self, prompt: str, project: Optional[str] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        # Fail loud (not silent) if the knowledge store was built with a
        # different embedding identity (backend/model/dimension) than the one
        # now configured. A mismatch means old rows are not comparable to the
        # probe vector, so query() would silently drop them from retrieval -
        # exactly the silent memory-loss failure we must not tolerate in
        # production. The caller must re-embed (or switch back) before retrieval.
        self.vector.check_compatibility(raise_on_mismatch=True)

        retrieval = self.rules["retrieval"]
        # `project` scopes Tier 1 to the resolved project plus global memory.
        # Without it, another project's knowledge/experience would be ranked
        # into this digest purely on cosine/token overlap.
        knowledge = self.vector.query(prompt, int(retrieval["vector_top_k"]), project=project)
        experience = self.graph.query(prompt, int(retrieval["graph_top_k"]), project=project)
        fallback: List[Dict[str, Any]] = []
        if self.use_ripgrep and not experience:
            # The graph knows nothing about this prompt; go look at the actual
            # repository rather than returning an empty Experience section.
            keyword = _primary_keyword(prompt)
            if keyword:
                fallback = ripgrep_fallback(keyword, top_k=int(retrieval["graph_top_k"]))
        return {"knowledge": knowledge, "experience": experience, "ripgrep": fallback}

    def merge(self, retrieved: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """Interleave both memory types into one ranked list."""
        merged: List[Dict[str, Any]] = []
        for entry in retrieved.get("knowledge", []):
            merged.append(
                {
                    "type": "knowledge",
                    "text": entry["text"],
                    "score": round(entry.get("score", 0.0) * _KNOWLEDGE_WEIGHT, 6),
                    "source": entry.get("source"),
                    "ts": entry.get("ts"),
                }
            )
        for entry in retrieved.get("experience", []):
            merged.append(
                {
                    "type": "experience",
                    "text": entry["text"],
                    "score": round(entry.get("score", 0.0) * _EXPERIENCE_WEIGHT, 6),
                    "source": entry.get("source"),
                    "ts": entry.get("ts"),
                    "relation": {"src": entry.get("src"), "rel": entry.get("rel"), "dst": entry.get("dst")},
                }
            )
        for entry in retrieved.get("ripgrep", []):
            merged.append(
                {
                    "type": "experience",
                    "text": "%s:%s  %s" % (entry["file"], entry["line"], entry["snippet"]),
                    "score": 0.0,
                    "source": "ripgrep",
                    "ts": None,
                    "origin": "ripgrep",
                }
            )
        merged.sort(key=lambda e: -e["score"])
        max_items = int(self.rules["retrieval"].get("merged_max_items", len(merged)))
        return merged[:max_items]

    # -- budgeting ---------------------------------------------------------
    def apply_budget(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Fit merged items into memory_char_limit.

        Items are admitted highest-score-first. The last item that does not fit
        whole is truncated to the remaining budget if a useful slice remains,
        otherwise it is dropped. The rendered block is hard-clamped afterwards,
        so the limit holds even if formatting overhead shifts.
        """
        limit = self.memory_char_limit
        kept: List[Dict[str, Any]] = []
        used = 0
        for item in items:
            bullet = _render_bullet(item)
            if used + len(bullet) <= limit:
                kept.append(item)
                used += len(bullet)
                continue
            remaining = limit - used
            # Only bother truncating if a meaningful fragment survives.
            if remaining >= 80:
                overhead = len(bullet) - len(item["text"])
                text_budget = remaining - overhead
                if text_budget >= 40:
                    clipped = dict(item)
                    clipped["text"] = truncate(item["text"], text_budget)
                    clipped["truncated"] = True
                    kept.append(clipped)
                    used += len(_render_bullet(clipped))
                    break  # budget is now exhausted; everything after is dropped
            # Item did not fit whole and could not be usefully clipped. Keep
            # going: a later, smaller item may still fit in the remainder.
        return {"items": kept, "dropped": len(items) - len(kept), "chars_used": used}

    def build(self, prompt: str) -> Dict[str, Any]:
        """Full pipeline for one prompt. Returns the JSON-shaped context."""
        prompt = prompt or ""
        prompt_slice = truncate(prompt, self.user_char_limit)
        retrieved = self.retrieve(prompt)
        merged = self.merge(retrieved)
        budgeted = self.apply_budget(merged)

        memory_block = _render_memory_block(budgeted["items"])
        # Belt and braces: the contract is on the emitted block, not on our
        # estimate of it.
        memory_block = truncate(memory_block, self.memory_char_limit)

        knowledge_items = [i for i in budgeted["items"] if i["type"] == "knowledge"]
        experience_items = [i for i in budgeted["items"] if i["type"] == "experience"]

        return {
            "version": self.rules.get("version", "0.1"),
            "prompt": prompt_slice,
            "prompt_truncated": len(prompt) > len(prompt_slice),
            "limits": {
                "user_char_limit": self.user_char_limit,
                "memory_char_limit": self.memory_char_limit,
            },
            "counts": {
                "knowledge": len(knowledge_items),
                "experience": len(experience_items),
                "dropped_for_budget": budgeted["dropped"],
                "candidates": len(merged),
            },
            "knowledge": knowledge_items,
            "experience": experience_items,
            "memory_block": memory_block,
            "memory_block_chars": len(memory_block),
            "prompt_chars": len(prompt_slice),
            "ripgrep_available": rg_available(),
        }

    # -- preflight (API_CONTRACTS.md section 2) ----------------------------
    def build_context(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Tiered preflight payload for POST /v1/context/build.

        Tier 0 (authority, roles, operating rules) comes from stable config -
        never from cosine recall, so it cannot be lost by a bad retrieval.
        Tier 1 is retrieved and scoped to the resolved project. Tier 2 (full
        conversations, historical detail) is deliberately NOT injected; it is
        reachable only through /v1/memory/search.

        Raises ValueError on an embedding-identity mismatch (mapped to HTTP 409
        by the API) - never returns a silently degraded digest.
        """
        request = dict(request or {})
        prompt = str(request.get("prompt") or "")
        warnings: List[str] = []
        provenance: List[Dict[str, Any]] = []

        # Budget: the caller's token_budget is advisory and is converted to
        # characters, but the configured hard caps always win.
        budget_chars = self.memory_char_limit
        token_budget = request.get("token_budget")
        if token_budget is not None:
            try:
                # ~4 chars/token is the standard rough conversion; deliberately
                # conservative so we undershoot rather than overshoot.
                requested = int(token_budget) * 4
                if requested < budget_chars:
                    budget_chars = max(0, requested)
            except (TypeError, ValueError):
                warnings.append("invalid token_budget ignored")

        identity_cfg = load_identity()
        if not identity_cfg:
            warnings.append("identity_config_unavailable")

        resolved_project = self._resolve_project(request, identity_cfg)

        # -- Tier 1 retrieval (fails loud on identity mismatch) -------------
        # Scoped to the resolved project: cross-project memory must never be
        # injected into an agent's prompt.
        retrieved = self.retrieve(prompt, project=resolved_project)
        merged = self.merge(retrieved)
        budgeted = self._apply_budget_chars(merged, budget_chars)

        knowledge_items = [i for i in budgeted["items"] if i["type"] == "knowledge"]
        experience_items = [i for i in budgeted["items"] if i["type"] == "experience"]

        # Decisions/tasks are structured memory, filtered by explicit status:
        # a proposal must never be presented as an approved decision.
        decisions = self._structured(resolved_project, "decision", "approved", prompt, warnings=warnings)
        open_tasks = self._structured(resolved_project, "task", "proposed", prompt, warnings=warnings)

        for entry in knowledge_items:
            provenance.append(self._provenance_of(entry, "knowledge"))
        for entry in experience_items:
            provenance.append(self._provenance_of(entry, "experience"))
        for entry in decisions:
            provenance.append(self._provenance_of(entry, "decision"))
        for entry in open_tasks:
            provenance.append(self._provenance_of(entry, "task"))

        if retrieved.get("ripgrep"):
            warnings.append("graph_empty_used_ripgrep_fallback")

        identity = {
            "backend": self.vector.embedder.name,
            "model": self.vector.embedder.model,
            "dim": self.vector.embedder.dimensions,
        }

        tier0 = self._tier0(identity_cfg, resolved_project)
        context_markdown = self._render_digest(
            tier0=tier0,
            prompt=truncate(prompt, self.user_char_limit),
            items=budgeted["items"],
            decisions=decisions,
            open_tasks=open_tasks,
            budget_chars=budget_chars,
        )
        # Hard clamp: the response must never exceed the requested budget.
        digest_cap = self._digest_limit(budget_chars, token_budget)
        if len(context_markdown) > digest_cap:
            # Say so rather than quietly shipping a clipped digest - the caller
            # needs to know memory was cut to fit, not assume it saw everything.
            warnings.append("context_truncated_to_token_budget")
        context_markdown = truncate(context_markdown, digest_cap)

        return {
            "request_id": request.get("request_id"),
            "resolved_project": resolved_project,
            "identity": identity,
            "version": SERVICE_VERSION,
            "tier0": tier0,
            "active_projects": [resolved_project] if resolved_project else [],
            "relevant_experiences": experience_items,
            "relevant_knowledge": knowledge_items,
            "relevant_decisions": decisions,
            "open_tasks": open_tasks,
            "relations": [
                i.get("relation") for i in experience_items if i.get("relation")
            ],
            "recommended_agent": self._recommend_agent(prompt, identity_cfg),
            "context_markdown": context_markdown,
            "context_chars": len(context_markdown),
            "memory_char_budget": budget_chars,
            "counts": {
                "knowledge": len(knowledge_items),
                "experience": len(experience_items),
                "decisions": len(decisions),
                "open_tasks": len(open_tasks),
                "dropped_for_budget": budgeted["dropped"],
                "candidates": len(merged),
            },
            "provenance": provenance,
            "warnings": warnings,
        }

    def _digest_limit(self, budget_chars: int, token_budget: Optional[int]) -> int:
        """Hard cap for the whole rendered digest.

        API_CONTRACTS.md: "The response must never exceed the requested budget
        when one is supplied." So when the caller supplies ``token_budget`` the
        cap is exactly that budget in characters - Tier 0 included. Tier 0 is
        rendered first, so a very small budget keeps the authority block and
        drops retrieved memory, which is the right way round: losing a decision
        is recoverable, losing "Faisal is the final authority" is not.

        With no token_budget, the configured memory cap applies plus a bounded
        frame for the Tier 0 block and headings.
        """
        if token_budget is not None:
            return max(0, budget_chars)
        return budget_chars + self.user_char_limit + 1200

    def _apply_budget_chars(self, items: List[Dict[str, Any]], limit: int) -> Dict[str, Any]:
        """apply_budget() against an explicit limit (per-request budget)."""
        saved = self.memory_char_limit
        try:
            self.memory_char_limit = min(saved, max(0, int(limit)))
            return self.apply_budget(items)
        finally:
            self.memory_char_limit = saved

    def _resolve_project(self, request: Dict[str, Any], identity_cfg: Dict[str, Any]) -> Optional[str]:
        """Resolve the active project from explicit hints only.

        Guessing a project from loose prompt text would scope memory to the
        wrong repository, so an unresolved project stays None rather than being
        invented.
        """
        for key in ("project_hint", "active_workspace"):
            value = request.get(key)
            if value:
                return str(value).strip().split("/")[-1] or None
        default = (identity_cfg.get("default_project") or "").strip()
        return default or None

    def _structured(
        self,
        project: Optional[str],
        memory_type: str,
        status: str,
        prompt: str,
        limit: int = 5,
        warnings: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Tier 1 structured lookup (decisions / tasks) by type and status.

        A failure here does not abort the preflight - a digest without the
        decision table is still useful - but it is never silent: the caller
        gets a warning so an empty list cannot be mistaken for "no decisions
        exist". An embedding-identity ValueError is re-raised so the 409
        fail-loud path still fires.
        """
        filters: Dict[str, Any] = {"memory_type": memory_type, "status": status}
        if project:
            filters["project"] = project
        try:
            return self.vector.search(query=prompt or None, filters=filters, limit=limit)
        except ValueError:
            # Identity mismatch: must reach the caller as a hard 409.
            raise
        except Exception as exc:
            if warnings is not None:
                warnings.append(
                    "%s_lookup_failed:%s" % (memory_type, type(exc).__name__)
                )
            return []

    @staticmethod
    def _provenance_of(entry: Dict[str, Any], memory_type: str) -> Dict[str, Any]:
        return {
            "memory_id": entry.get("id"),
            "type": memory_type,
            "status": entry.get("status", "approved"),
            "source_type": entry.get("source_type") or entry.get("source") or "conversation",
            "source_id": entry.get("source_id") or entry.get("source"),
            "created_at": entry.get("ts"),
            "confidence": entry.get("confidence"),
            "score": entry.get("score"),
        }

    @staticmethod
    def _tier0(identity_cfg: Dict[str, Any], project: Optional[str]) -> Dict[str, Any]:
        """Always-loaded context from stable config (never semantic recall)."""
        authority = identity_cfg.get("authority") or {}
        return {
            "final_authority": authority.get("final_authority", "Faisal"),
            "approval_policy": authority.get(
                "policy",
                "No commit, push, merge, deploy or data deletion without explicit approval.",
            ),
            "jebat_identity": identity_cfg.get("jebat_identity") or {},
            "roles": identity_cfg.get("roles") or [],
            "operating_rules": identity_cfg.get("operating_rules") or [],
            "active_project": project,
        }

    @staticmethod
    def _recommend_agent(prompt: str, identity_cfg: Dict[str, Any]) -> str:
        """Pick an agent from configured keyword routing. Defaults to jebat."""
        default = identity_cfg.get("default_agent") or "jebat"
        tokens = set(tokenize(prompt))
        if not tokens:
            return default
        best = default
        best_hits = 0
        for route in identity_cfg.get("agent_routing") or []:
            hits = len(tokens & {str(k).lower() for k in route.get("keywords", [])})
            if hits > best_hits:
                best_hits = hits
                best = route.get("agent", default)
        return best

    def _render_digest(
        self,
        tier0: Dict[str, Any],
        prompt: str,
        items: List[Dict[str, Any]],
        decisions: List[Dict[str, Any]],
        open_tasks: List[Dict[str, Any]],
        budget_chars: int,
    ) -> str:
        """Markdown digest: Tier 0 first (it must survive truncation)."""
        lines = ["## Jebat-Cortex Context Digest", ""]
        lines.append("### Tier 0 - Always Loaded")
        lines.append("- Final authority: %s" % tier0.get("final_authority"))
        lines.append("- Approval policy: %s" % tier0.get("approval_policy"))
        if tier0.get("active_project"):
            lines.append("- Active project: %s" % tier0["active_project"])
        for rule in (tier0.get("operating_rules") or [])[:5]:
            lines.append("- Rule: %s" % rule)
        lines.append("")

        if decisions:
            lines.append("### Approved Decisions")
            for entry in decisions:
                lines.append("- %s" % entry.get("text", ""))
            lines.append("")

        if open_tasks:
            lines.append("### Open Tasks")
            for entry in open_tasks:
                lines.append("- %s" % entry.get("text", ""))
            lines.append("")

        lines.append("### Tier 1 - Retrieved Memory")
        body = _render_memory_block(items).strip()
        lines.append(body if body else "_no memory matched this prompt_")
        lines.append("")
        lines.append("_Tier 2 (full conversations, historical detail) not injected; "
                     "query /v1/memory/search on demand._")
        return "\n".join(lines) + "\n"

    def to_markdown(self, context: Dict[str, Any]) -> str:
        """Human/prompt-facing digest. The memory block stays within budget."""
        lines = [
            "## Jebat-Cortex Memory Digest",
            "",
            "**Prompt:** %s" % context["prompt"],
            "",
            "**Budget:** memory %d/%d chars | prompt %d/%d chars | knowledge %d | experience %d"
            % (
                context["memory_block_chars"],
                context["limits"]["memory_char_limit"],
                context["prompt_chars"],
                context["limits"]["user_char_limit"],
                context["counts"]["knowledge"],
                context["counts"]["experience"],
            ),
            "",
            "### Memory",
            "",
        ]
        body = context["memory_block"].strip()
        lines.append(body if body else "_no memory matched this prompt_")
        if context["counts"]["dropped_for_budget"]:
            lines.extend(
                ["", "_%d item(s) dropped to respect the memory budget._"
                 % context["counts"]["dropped_for_budget"]]
            )
        return "\n".join(lines) + "\n"


def _render_bullet(item: Dict[str, Any]) -> str:
    tag = "K" if item["type"] == "knowledge" else "E"
    return "- [%s] %s\n" % (tag, item["text"])


def _render_memory_block(items: List[Dict[str, Any]]) -> str:
    return "".join(_render_bullet(item) for item in items)


def _primary_keyword(prompt: str) -> str:
    """Longest meaningful token in the prompt - the best single rg probe."""
    from core.rules import tokenize

    tokens = [t for t in tokenize(prompt) if len(t) >= 4]
    if not tokens:
        return ""
    return max(tokens, key=len)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Jebat-Cortex Context Builder")
    parser.add_argument("prompt", nargs="+", help="the new prompt to build context for")
    parser.add_argument("--rules", default=None, help="path to cortex_rules.json")
    parser.add_argument("--vector-db", default=None, help="override vector store path")
    parser.add_argument("--graph-db", default=None, help="override graph store path")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    parser.add_argument("--both", action="store_true", help="emit Markdown then JSON")
    parser.add_argument("--no-ripgrep", action="store_true", help="disable the ripgrep fallback")
    args = parser.parse_args(argv)

    rules = load_rules(args.rules)
    vector = VectorStore(args.vector_db, rules=rules) if args.vector_db else None
    graph = GraphStore(args.graph_db, rules=rules) if args.graph_db else None
    builder = ContextBuilder(
        rules=rules,
        vector_store=vector,
        graph_store=graph,
        use_ripgrep=not args.no_ripgrep,
    )
    try:
        context = builder.build(" ".join(args.prompt))
        if args.json:
            print(json.dumps(context, indent=2))
        elif args.both:
            sys.stdout.write(builder.to_markdown(context))
            print()
            print(json.dumps(context, indent=2))
        else:
            sys.stdout.write(builder.to_markdown(context))
    finally:
        if vector is not None:
            vector.close()
        if graph is not None:
            graph.close()
        builder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
