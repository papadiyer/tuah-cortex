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
from core.rules import keyword_overlap, load_rules, repo_path, tokenize
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
        self.vector = vector_store if vector_store is not None else VectorStore(rules=self.rules)
        self.graph = graph_store if graph_store is not None else GraphStore(rules=self.rules)
        self._owns_stores = vector_store is None and graph_store is None
        cls = self.rules["classification"]
        self._experience_keywords = cls["experience_keywords"]
        self._knowledge_keywords = cls["knowledge_keywords"]
        self._experience_patterns = [re.compile(p) for p in cls["experience_patterns"]]

    def close(self) -> None:
        if self._owns_stores:
            self.vector.close()
            self.graph.close()

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
            record: Dict[str, Any] = {
                "text": segment,
                "kind": kind,
                "role": role,
                "ts": ts,
                "source": source,
                "scores": self.score_segment(segment),
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
                    {"source": record["source"], "ts": record["ts"], "role": record["role"]},
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
                        meta={"ts": record["ts"], "role": record["role"]},
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
