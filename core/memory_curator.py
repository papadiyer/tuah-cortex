"""Memory Curator: parse a conversation log and split it into Knowledge and
Experience, routing each into the correct store.

Input log format: JSONL, one message object per line::

    {"role": "user", "content": "...", "ts": "2026-08-06T10:00:00Z"}

Run as::

    python3 -m core.memory_curator data/sample_conversation.jsonl

Async (asyncio) because classification of independent segments is embarrassingly
parallel and the real pipeline will later call out to I/O-bound services; the
current classifiers are CPU-cheap and run in the event loop directly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from core.graph_store import GraphStore
from core.rules import active_axes, keyword_overlap, load_rules, repo_path, score_axes, tokenize
from core.vector_store import VectorStore

# Relation cues -> edge type. Order matters: first match wins.
_RELATION_PATTERNS: List[Tuple[str, str]] = [
    (r"\bimports?\b|\bimported by\b", "imports"),
    (r"\bdepends? on\b|\bdependency\b|\brequires?\b", "depends_on"),
    (r"\bcalls?\b|\binvokes?\b", "calls"),
    (r"\bextends?\b|\binherits? from\b|\bsubclass(?:es)? of\b", "extends"),
    (r"\bwrites? to\b|\bpersists? (?:to|into)\b|\bstores? (?:to|in|into)\b", "writes_to"),
    (r"\breads? from\b|\bloads? from\b", "reads_from"),
    (r"\btests?\b|\bcovers?\b", "tests"),
    (r"\bcontains?\b|\bincludes?\b|\bhas\b", "contains"),
    (r"\buses?\b|\bwired to\b|\bconnects? to\b", "uses"),
]

_FILE_RE = re.compile(
    r"[\w./-]+\.(?:py|js|ts|tsx|json|jsonl|sh|md|ya?ml|toml|sql|txt|db|sqlite3?|csv|cfg|ini)\b"
)

# Cue words that signal a relation, never a useful object label on their own.
_RELATION_CUE_WORDS = frozenset(
    """import imports imported depend depends dependency requires require call calls
    invoke invokes extend extends inherit inherits subclass write writes persist
    persists store stores read reads load loads test tests cover covers contain
    contains include includes has use uses wired connect connects""".split()
)
_MODULE_RE = re.compile(r"(?m)^\s*(?:from|import)\s+([A-Za-z_][\w.]*)")

# Which postflight memory types count as Experience for the cto/founder gate.
# process_message() has a literal "experience"/"knowledge" kind; an event does
# not, so the mapping is stated here instead of being inferred per call site.
# A `decision` qualifies because EXPERT_AXIS_ROUTING_v0.5.md section 1 scopes the
# cto tag to "architecture trade-off, build-vs-buy, governance, product
# decisions, failure lessons" - a recorded decision is a judgement, not a
# neutral fact. Lessons/tasks stay knowledge-class and can never be tagged
# cto/founder.
_EXPERIENCE_CLASS_TYPES = frozenset({"experience", "decision"})
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


class Curator:
    """Classifies conversation segments and routes them into the two stores."""

    def __init__(
        self,
        rules: Optional[dict] = None,
        vector_store: Optional[VectorStore] = None,
        graph_store: Optional[GraphStore] = None,
    ):
        self.rules = rules or load_rules()
        # Per-store ownership (see core/context_builder.py for the rationale):
        # injecting exactly one store used to leave the other one unclosed.
        self._owns_vector = vector_store is None
        self._owns_graph = graph_store is None
        self.vector = vector_store if vector_store is not None else VectorStore(rules=self.rules)
        try:
            self.graph = graph_store if graph_store is not None else GraphStore(rules=self.rules)
        except Exception:
            if self._owns_vector:
                try:
                    self.vector.close()
                except Exception:
                    pass
            raise
        self._closed = False
        cls = self.rules["classification"]
        self._experience_keywords = cls["experience_keywords"]
        self._knowledge_keywords = cls["knowledge_keywords"]
        self._experience_patterns = [re.compile(p) for p in cls["experience_patterns"]]
        # Optional section: a config without expert_axes simply tags nothing,
        # which is exactly pre-v0.5 behaviour.
        self._expert_axes = self.rules.get("expert_axes") or {}

    @property
    def own_stores(self) -> Dict[str, Any]:
        """Store handles whose lifecycle this curator is responsible for."""
        owned: Dict[str, Any] = {}
        if self._owns_vector:
            owned["vector_store"] = self.vector
        if self._owns_graph:
            owned["graph_store"] = self.graph
        return owned

    def close(self) -> None:
        """Close every store this curator owns. Idempotent."""
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

    def __enter__(self) -> "Curator":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- classification ----------------------------------------------------
    def score_segment(self, text: str) -> Dict[str, float]:
        """Return raw experience/knowledge scores for a segment."""
        experience = float(keyword_overlap(text, self._experience_keywords))
        knowledge = float(keyword_overlap(text, self._knowledge_keywords))
        # Structural patterns (paths, imports, code fences) are strong evidence
        # of Experience - weight them above bare keyword hits.
        for pattern in self._experience_patterns:
            if pattern.search(text):
                experience += 2.0
        return {"experience": experience, "knowledge": knowledge}

    def score_experts(self, text: str, kind: Optional[str] = None) -> Dict[str, float]:
        """Expert-axis confidences for a segment, in 0..1.

        Thin wrapper over ``rules.score_axes`` - the same scorer the context
        builder routes with, so a memory is tagged by exactly the rule that will
        later retrieve it.

        ``kind`` enforces the Experience gate: cto/founder never attach to a
        knowledge segment (EXPERT_AXIS_ROUTING_v0.5.md section 3). Callers that
        omit it get the ungated score, so this stays useful for inspection.
        """
        return score_axes(text, self._expert_axes, kind=kind)

    def expert_tags(self, text: str, kind: Optional[str] = None) -> Tuple[List[str], Dict[str, float]]:
        """(axes above threshold, confidence per axis) for one segment."""
        scores = self.score_experts(text, kind=kind)
        return active_axes(scores, self._expert_axes), scores

    def classify(self, text: str) -> str:
        """'experience' or 'knowledge'. Ties fall back to knowledge."""
        scores = self.score_segment(text)
        margin = float(self.rules["curator"].get("classification_margin", 0.0))
        if scores["experience"] > scores["knowledge"] + margin:
            return "experience"
        return "knowledge"

    # -- extraction --------------------------------------------------------
    def extract_relations(self, text: str, source: Optional[str] = None) -> List[Dict[str, str]]:
        """Pull (src, rel, dst) triples out of an Experience segment.

        Heuristic and deliberately conservative: files/modules named in the
        segment become nodes; an explicit relation cue links the first two.
        """
        files = _FILE_RE.findall(text)
        modules = _MODULE_RE.findall(text)
        entities: List[str] = []
        for item in files + modules:
            if item not in entities:
                entities.append(item)

        rel = "mentions"
        for pattern, name in _RELATION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                rel = name
                break

        triples: List[Dict[str, str]] = []
        if len(entities) >= 2:
            for dst in entities[1:]:
                triples.append({"src": entities[0], "rel": rel, "dst": dst})
        elif len(entities) == 1:
            topic = self._topic_label(text, exclude=entities[0])
            if topic:
                triples.append({"src": entities[0], "rel": rel, "dst": topic})
        return triples

    @staticmethod
    def _topic_label(text: str, exclude: str = "") -> str:
        """Pick a short label for the concept a single-entity segment is about.

        Relation cue words ("writes", "imports", ...) are skipped: they describe
        the edge, so using one as the object yields a degenerate triple such as
        ``memory_curator.py writes_to writes``.
        """
        for token in tokenize(text):
            if token == exclude or "." in token or len(token) < 4:
                continue
            if token in _RELATION_CUE_WORDS:
                continue
            return token
        return ""

    def segment_message(self, content: str) -> List[str]:
        """Split a message into classifiable segments (sentence-ish chunks)."""
        cfg = self.rules["curator"]
        min_chars = int(cfg["min_segment_chars"])
        max_chars = int(cfg["max_segment_chars"])

        # Keep fenced code blocks intact; they are single Experience units.
        parts: List[str] = []
        for block in re.split(r"(```.*?```)", content or "", flags=re.DOTALL):
            block = block.strip()
            if not block:
                continue
            if block.startswith("```"):
                parts.append(block)
                continue
            for sentence in _SENTENCE_SPLIT_RE.split(block):
                sentence = sentence.strip()
                if sentence:
                    parts.append(sentence)

        segments: List[str] = []
        for part in parts:
            if len(part) < min_chars:
                continue
            while len(part) > max_chars:
                segments.append(part[:max_chars])
                part = part[max_chars:]
            if len(part) >= min_chars:
                segments.append(part)
        return segments

    # -- async pipeline ----------------------------------------------------
    async def process_message(self, message: Dict[str, Any], source: str) -> List[Dict[str, Any]]:
        """Classify every segment of one message. Returns routed records."""
        content = message.get("content") or ""
        role = message.get("role") or "unknown"
        ts = message.get("ts")
        if role in self.rules["curator"].get("skip_roles", []):
            return []

        records: List[Dict[str, Any]] = []
        for segment in self.segment_message(content):
            await asyncio.sleep(0)  # cooperative yield; keeps the loop fair
            kind = self.classify(segment)
            experts, expert_confidence = self.expert_tags(segment, kind=kind)
            record: Dict[str, Any] = {
                "text": segment,
                "kind": kind,
                "role": role,
                "ts": ts,
                "source": source,
                "scores": self.score_segment(segment),
                "experts": experts,
                "expert_confidence": expert_confidence,
            }
            if kind == "experience":
                record["relations"] = self.extract_relations(segment, source)
            records.append(record)
        return records

    async def ingest(self, log_path: str) -> Dict[str, Any]:
        """Read a JSONL conversation log and persist both memory types."""
        # Fail loud (not silent) if the knowledge store already holds vectors
        # from a different embedding backend/dimension than the active one.
        # Mixing them would make cosine scores meaningless; the caller must use
        # a fresh db or flip the backend back.
        try:
            self.vector.check_compatibility(raise_on_mismatch=True)
        except ValueError as exc:
            raise ValueError("cannot ingest into incompatible vector store: %s" % exc)

        messages, malformed = read_log(log_path)
        source = os.path.basename(log_path)

        batches = await asyncio.gather(
            *(self.process_message(message, source) for message in messages)
        )
        records = [record for batch in batches for record in batch]

        knowledge_added = 0
        experience_edges = 0
        experience_nodes = 0
        for record in records:
            if record["kind"] == "knowledge":
                row_id = self.vector.add(
                    record["text"],
                    {
                        "source": record["source"],
                        "ts": record["ts"],
                        "role": record["role"],
                        "experts": record["experts"],
                        "expert_confidence": record["expert_confidence"],
                    },
                )
                if row_id is not None:
                    knowledge_added += 1
            else:
                relations = record.get("relations") or []
                if not relations:
                    # No triple extracted: keep the fact as a concept node so it
                    # is not silently lost.
                    label = self._topic_label(record["text"]) or record["text"][:60]
                    if self.graph.add_node(label, kind="concept") is not None:
                        experience_nodes += 1
                    continue
                for triple in relations:
                    edge_id = self.graph.add_edge(
                        triple["src"],
                        triple["rel"],
                        triple["dst"],
                        source=record["source"],
                        meta={
                            "ts": record["ts"],
                            "role": record["role"],
                            "experts": record["experts"],
                            "expert_confidence": record["expert_confidence"],
                        },
                    )
                    if edge_id is not None:
                        experience_edges += 1

        return {
            "log": log_path,
            "messages": len(messages),
            "malformed_lines": malformed,
            "segments": len(records),
            "knowledge_segments": sum(1 for r in records if r["kind"] == "knowledge"),
            "experience_segments": sum(1 for r in records if r["kind"] == "experience"),
            "knowledge_added": knowledge_added,
            "experience_edges": experience_edges,
            "experience_nodes": experience_nodes,
            "vector_total": self.vector.count(),
            "graph_nodes_total": self.graph.count_nodes(),
            "graph_edges_total": self.graph.count_edges(),
        }


    # -- postflight events -------------------------------------------------
    def ingest_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Ingest one postflight event (EVENT_SCHEMA.md section 1).

        A thin adapter over the same stores ``ingest()`` writes to - it does not
        re-implement curation. Structured parts of the event carry explicit
        provenance and status, so a *proposed* decision is stored as proposed
        and never surfaces as approved (MEMORY_SCHEMA.md section 1).

        Synchronous: the worker already owns a thread, and the stores are local
        SQLite. Returns a per-type count summary.
        """
        event = dict(event or {})
        # Fail loud rather than silently writing into a store whose vectors are
        # not comparable with the active embedder.
        try:
            self.vector.check_compatibility(raise_on_mismatch=True)
        except ValueError as exc:
            raise ValueError("cannot ingest event into incompatible vector store: %s" % exc)

        event_id = str(event.get("event_id") or "")
        project = event.get("project")
        timestamp = event.get("timestamp")
        agent = event.get("agent") or "unknown"

        def _provenance(memory_type: str, status: str, extra: Optional[Dict[str, Any]] = None):
            meta = {
                # VectorStore fingerprints on "source::text" to suppress
                # re-ingesting the same log line twice. Scoping `source` to this
                # event keeps that protection *within* an event while allowing
                # two different events (or two projects) to record the same
                # sentence. With a constant "agent_event" source, the second
                # event's identical decision was silently dropped - memory loss
                # reported as success, which this codebase must never do.
                "source": "agent_event:%s" % (event_id or "unknown"),
                "source_type": "agent_event",
                "source_id": event_id,
                "ts": timestamp,
                "status": status,
                "project": project,
                "type": memory_type,
                "agent": agent,
            }
            if extra:
                meta.update(extra)
            return meta

        counts = {
            "decisions": 0,
            "lessons": 0,
            "open_tasks": 0,
            "artefacts": 0,
            "experience_edges": 0,
            "summary": 0,
        }
        # Items the store refused as duplicates. Reported, never hidden: a
        # caller must be able to tell "stored 3 of 3" from "stored 2 of 3".
        skipped: List[Dict[str, str]] = []

        def _store(kind: str, text: str, meta: Dict[str, Any]) -> None:
            # Tag with the same scorer used by conversation ingest. The gate key
            # is the memory's own type, so a `lesson` cannot pick up a cto tag
            # while a `decision` can.
            gate = (
                "experience"
                if str(meta.get("type")) in _EXPERIENCE_CLASS_TYPES
                else "knowledge"
            )
            experts, expert_confidence = self.expert_tags(text, kind=gate)
            if experts:
                meta = dict(meta)
                meta["experts"] = experts
                meta["expert_confidence"] = expert_confidence
            if self.vector.add(text, meta) is not None:
                counts[kind] += 1
            else:
                skipped.append({"kind": kind, "text": text[:120], "reason": "duplicate_fingerprint"})

        # Decisions -> knowledge rows tagged with their real status.
        for decision in event.get("decisions") or []:
            text = (decision or {}).get("text", "").strip()
            if not text:
                continue
            status = (decision.get("status") or "proposed").strip().lower()
            meta = _provenance("decision", status, {"project": decision.get("project") or project})
            _store("decisions", text, meta)

        # Lessons are knowledge the runtime learned; approved by definition once
        # the work completed, but still attributed to the event.
        for lesson in event.get("lessons") or []:
            text = (lesson or {}).get("text", "").strip()
            if not text:
                continue
            meta = _provenance("knowledge", "approved", {"project": lesson.get("project") or project})
            _store("lessons", text, meta)

        # Open tasks are unresolved work: status 'proposed' until completed.
        for task in event.get("open_tasks") or []:
            title = (task or {}).get("title", "").strip()
            if not title:
                continue
            status = (task.get("status") or "open").strip().lower()
            normalised = "completed" if status == "completed" else "proposed"
            meta = _provenance("task", normalised, {"project": task.get("project") or project})
            _store("open_tasks", title, meta)

        # Artefacts are structural facts -> experience graph.
        for artefact in event.get("artefacts") or []:
            ref = (artefact or {}).get("path_or_ref", "").strip()
            if not ref:
                continue
            kind = (artefact.get("kind") or "file").strip() or "file"
            # Artefacts are structural Experience, so the gate is open here.
            art_experts, art_confidence = self.expert_tags(ref, kind="experience")
            edge_id = self.graph.add_edge(
                agent,
                "produced",
                ref,
                source="agent_event",
                meta={
                    "ts": timestamp,
                    "status": "approved",
                    "source_type": "agent_event",
                    "project": project,
                    "kind": kind,
                    "experts": art_experts,
                    "expert_confidence": art_confidence,
                },
            )
            if edge_id is not None:
                counts["artefacts"] += 1

        # The result summary is the event's own memory. Stored as knowledge so a
        # later prompt can recall what was done, with the event's status.
        summary = (event.get("result_summary") or "").strip()
        if summary:
            status = "approved" if (event.get("status") or "completed") == "completed" else "rejected"
            _store("summary", summary, _provenance("experience", status))

        return {
            "event_id": event_id,
            "request_id": event.get("request_id"),
            "project": project,
            "counts": counts,
            "skipped": skipped,
            "skipped_count": len(skipped),
            "ingested": True,
            "vector_total": self.vector.count(),
            "graph_edges_total": self.graph.count_edges(),
        }


def read_log(log_path: str) -> Tuple[List[Dict[str, Any]], int]:
    """Parse a JSONL log. Returns (messages, malformed_line_count).

    Malformed lines are counted and skipped, never silently swallowed - the
    count is reported back to the caller.
    """
    if not os.path.exists(log_path):
        raise FileNotFoundError("conversation log not found: %s" % log_path)
    messages: List[Dict[str, Any]] = []
    malformed = 0
    with open(log_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if isinstance(obj, dict) and "content" in obj:
                messages.append(obj)
            else:
                malformed += 1
    return messages, malformed


async def main_async(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Jebat-Cortex Memory Curator")
    parser.add_argument("log", help="path to a JSONL conversation log")
    parser.add_argument("--rules", default=None, help="path to cortex_rules.json")
    parser.add_argument("--vector-db", default=None, help="override vector store path")
    parser.add_argument("--graph-db", default=None, help="override graph store path")
    args = parser.parse_args(argv)

    rules = load_rules(args.rules)
    vector = VectorStore(args.vector_db, rules=rules) if args.vector_db else None
    graph = GraphStore(args.graph_db, rules=rules) if args.graph_db else None
    curator = Curator(rules=rules, vector_store=vector, graph_store=graph)
    try:
        report = await curator.ingest(args.log)
    except FileNotFoundError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    finally:
        if vector is not None:
            vector.close()
        if graph is not None:
            graph.close()
        curator.close()

    print(json.dumps(report, indent=2))
    if report["malformed_lines"]:
        print(
            "warning: skipped %d malformed line(s)" % report["malformed_lines"],
            file=sys.stderr,
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    raise SystemExit(main())
