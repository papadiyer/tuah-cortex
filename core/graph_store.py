"""SQLite-backed graph store for Experience entries (structural/relational).

Nodes are entities (files, modules, classes, concepts); edges are typed
relations between them (file -imports-> module, entity -rel-> entity).
Query does keyword matching over node labels and edge relations, scored by
overlap. A ripgrep fallback returns real file/line references when the graph
does not know about a keyword; it degrades gracefully when `rg` is absent.
"""

from __future__ import annotations

import os
import json
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.rules import load_rules, repo_path, tokenize

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    kind  TEXT NOT NULL DEFAULT 'entity',
    meta  TEXT,
    ts    TEXT,
    UNIQUE(label, kind)
);
CREATE TABLE IF NOT EXISTS edges (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    src      INTEGER NOT NULL REFERENCES nodes(id),
    dst      INTEGER NOT NULL REFERENCES nodes(id),
    rel      TEXT NOT NULL,
    source   TEXT,
    ts       TEXT,
    meta     TEXT,
    UNIQUE(src, dst, rel)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
"""

# Bounds for the ripgrep fallback so a broad keyword cannot flood the caller.
_RG_MAX_COUNT = 20
_RG_TIMEOUT_SEC = 10


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GraphStore:
    """Relational/code-graph memory with a ripgrep fallback."""

    def __init__(self, db_path: Optional[str] = None, rules: Optional[dict] = None):
        self.rules = rules or load_rules()
        if db_path is None:
            db_path = repo_path(self.rules["paths"]["graph_db"])
        self.db_path = db_path
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- writes ------------------------------------------------------------
    def add_node(
        self,
        label: str,
        kind: str = "entity",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Insert or fetch a node. Returns its id, or None for an empty label."""
        label = (label or "").strip()
        if not label:
            return None
        self.conn.execute(
            "INSERT OR IGNORE INTO nodes (label, kind, meta, ts) VALUES (?, ?, ?, ?)",
            (label, kind, json.dumps(meta or {}, sort_keys=True), _utc_now()),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM nodes WHERE label = ? AND kind = ?", (label, kind)
        ).fetchone()
        return int(row["id"]) if row else None

    def add_edge(
        self,
        src_label: str,
        rel: str,
        dst_label: str,
        src_kind: str = "entity",
        dst_kind: str = "entity",
        source: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Insert a typed edge, creating endpoint nodes as needed."""
        rel = (rel or "").strip()
        if not rel:
            return None
        src = self.add_node(src_label, src_kind)
        dst = self.add_node(dst_label, dst_kind)
        if src is None or dst is None:
            return None
        self.conn.execute(
            "INSERT OR IGNORE INTO edges (src, dst, rel, source, ts, meta)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (src, dst, rel, source, _utc_now(), json.dumps(meta or {}, sort_keys=True)),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM edges WHERE src = ? AND dst = ? AND rel = ?", (src, dst, rel)
        ).fetchone()
        return int(row["id"]) if row else None

    # -- reads -------------------------------------------------------------
    def count_nodes(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])

    def count_edges(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0])

    def neighbors(self, label: str) -> List[Dict[str, Any]]:
        """All edges where ``label`` is the source or destination."""
        rows = self.conn.execute(
            "SELECT s.label AS src, e.rel AS rel, d.label AS dst, e.source AS source,"
            "       e.ts AS ts"
            " FROM edges e"
            " JOIN nodes s ON s.id = e.src"
            " JOIN nodes d ON d.id = e.dst"
            " WHERE s.label = ? OR d.label = ?"
            " ORDER BY e.id",
            (label, label),
        ).fetchall()
        return [dict(row) for row in rows]

    def query(self, subject_or_keyword: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        """Rank edges by keyword overlap with subject/relation/object labels."""
        if top_k is None:
            top_k = int(self.rules["retrieval"]["graph_top_k"])
        probe_tokens = set(tokenize(subject_or_keyword))
        if not probe_tokens:
            return []

        results: List[Dict[str, Any]] = []
        rows = self.conn.execute(
            "SELECT e.id AS id, s.label AS src, e.rel AS rel, d.label AS dst,"
            "       e.source AS source, e.ts AS ts"
            " FROM edges e"
            " JOIN nodes s ON s.id = e.src"
            " JOIN nodes d ON d.id = e.dst"
        ).fetchall()
        for row in rows:
            triple = "%s %s %s" % (row["src"], row["rel"], row["dst"])
            edge_tokens = set(tokenize(triple))
            if not edge_tokens:
                continue
            exact = probe_tokens & edge_tokens
            # Partial credit for substring hits (e.g. "vector" vs "vector_store.py").
            partial = 0.0
            for probe in probe_tokens - exact:
                if len(probe) >= 4 and any(probe in tok for tok in edge_tokens):
                    partial += 0.5
            score = (len(exact) + partial) / float(len(probe_tokens))
            if score <= 0:
                continue
            results.append(
                {
                    "id": int(row["id"]),
                    "src": row["src"],
                    "rel": row["rel"],
                    "dst": row["dst"],
                    "text": triple,
                    "source": row["source"],
                    "ts": row["ts"],
                    "type": "experience",
                    "score": round(score, 6),
                }
            )
        results.sort(key=lambda e: (-e["score"], e["id"]))
        return results[:top_k]


def ripgrep_fallback(
    keyword: str,
    repo: Optional[str] = None,
    top_k: int = 10,
) -> List[Dict[str, Any]]:
    """Return file/line references for ``keyword`` using ripgrep.

    Degrades gracefully: returns [] if `rg` is missing, the search times out,
    or ripgrep reports no matches. Never raises for the normal failure modes.
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    binary = shutil.which("rg")
    if binary is None:
        return []
    root = repo or repo_path()
    if not os.path.isdir(root):
        return []

    command = [
        binary,
        "--no-heading",
        "--line-number",
        "--fixed-strings",
        "--ignore-case",
        "--max-count", str(_RG_MAX_COUNT),
        "--max-columns", "300",
        "--", keyword, root,
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_RG_TIMEOUT_SEC,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    # rg exit code 1 == no matches (not an error); >1 == real failure.
    if completed.returncode > 1:
        return []

    hits: List[Dict[str, Any]] = []
    for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        path, lineno, snippet = parts
        if not lineno.isdigit():
            continue
        hits.append(
            {
                "file": os.path.relpath(path, root),
                "line": int(lineno),
                "snippet": snippet.strip()[:200],
                "type": "experience",
                "origin": "ripgrep",
            }
        )
        if len(hits) >= top_k:
            break
    return hits


def rg_available() -> bool:
    """True when the ripgrep binary can be found on PATH."""
    return shutil.which("rg") is not None


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    import sys

    store = GraphStore()
    if len(sys.argv) > 1:
        probe = " ".join(sys.argv[1:])
        for hit in store.query(probe):
            print("%.4f  %s" % (hit["score"], hit["text"]))
    else:
        print("nodes: %d  edges: %d" % (store.count_nodes(), store.count_edges()))
    store.close()
