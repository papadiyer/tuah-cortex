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
from core.rules import load_rules, truncate
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
        self.vector = vector_store if vector_store is not None else VectorStore(rules=self.rules)
        self.graph = graph_store if graph_store is not None else GraphStore(rules=self.rules)
        self._owns_stores = vector_store is None and graph_store is None
        self.use_ripgrep = use_ripgrep
        self.user_char_limit = int(self.rules["limits"]["user_char_limit"])
        self.memory_char_limit = int(self.rules["limits"]["memory_char_limit"])

    def close(self) -> None:
        if self._owns_stores:
            self.vector.close()
            self.graph.close()

    def __enter__(self) -> "ContextBuilder":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- retrieval ---------------------------------------------------------
    def retrieve(self, prompt: str) -> Dict[str, List[Dict[str, Any]]]:
        retrieval = self.rules["retrieval"]
        knowledge = self.vector.query(prompt, int(retrieval["vector_top_k"]))
        experience = self.graph.query(prompt, int(retrieval["graph_top_k"]))
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
