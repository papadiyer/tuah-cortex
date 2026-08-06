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

# Additive provenance/status columns (MEMORY_SCHEMA.md section 1 / section 5).
# `meta` already exists on both tables; status/project are promoted to columns
# so retrieval can filter without parsing every JSON blob.
_ADDITIVE_EDGE_COLUMNS = (
    ("status", "TEXT"),
    ("source_type", "TEXT"),
    ("project", "TEXT"),
)
_ADDITIVE_NODE_COLUMNS = (
    ("status", "TEXT"),
    ("project", "TEXT"),
)

# Replaced/rejected experience stays queryable for provenance but is excluded
# from default retrieval (MEMORY_SCHEMA.md section 3).
_EXCLUDED_BY_DEFAULT = ("superseded", "rejected")


def _ensure_columns(conn: "sqlite3.Connection") -> None:
    """Add additive columns to existing nodes/edges tables if absent.

    Databases created before provenance tagging are migrated in place rather
    than rejected. Pre-existing rows carry NULL status and are treated as
    'approved' by retrieval, since they predate the status model.
    """
    changed = False
    for table, columns in (("edges", _ADDITIVE_EDGE_COLUMNS), ("nodes", _ADDITIVE_NODE_COLUMNS)):
        existing = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        for name, coltype in columns:
            if name not in existing:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, coltype))
                changed = True
    if changed:
        conn.commit()

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
        # check_same_thread=False: see the note in core/vector_store.py - the
        # threaded HTTP server uses one store from many request threads, and
        # api.app serialises those calls with a lock.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        _ensure_columns(self.conn)
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
        payload = dict(meta or {})
        status = payload.pop("status", None) or "approved"
        project = payload.pop("project", None)
        self.conn.execute(
            "INSERT OR IGNORE INTO nodes (label, kind, meta, ts, status, project)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (label, kind, json.dumps(payload, sort_keys=True), _utc_now(), status, project),
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
        payload = dict(meta or {})
        status = payload.pop("status", None) or "approved"
        source_type = payload.pop("source_type", None) or "conversation"
        project = payload.pop("project", None)
        # Endpoint nodes inherit the edge's project scope so a project-filtered
        # graph query can reach them; status stays per-row.
        node_meta = {"project": project} if project else None
        src = self.add_node(src_label, src_kind, meta=node_meta)
        dst = self.add_node(dst_label, dst_kind, meta=node_meta)
        if src is None or dst is None:
            return None
        self.conn.execute(
            "INSERT OR IGNORE INTO edges"
            " (src, dst, rel, source, ts, meta, status, source_type, project)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                src,
                dst,
                rel,
                source,
                _utc_now(),
                json.dumps(payload, sort_keys=True),
                status,
                source_type,
                project,
            ),
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

    def related(
        self,
        entity: str,
        top_k: int = 10,
        project: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Graph neighbours of ``entity`` (MCP cortex.get_related_experiences).

        Matches the label exactly or as a substring, so "vector_store" finds
        "core/vector_store.py". Excludes superseded/rejected edges.
        """
        entity = (entity or "").strip()
        if not entity:
            return []
        # Escape LIKE metacharacters so entity='%' searches for a literal
        # percent sign instead of matching every edge in the graph.
        literal = (
            entity.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        like = "%%%s%%" % literal
        sql = (
            "SELECT e.id AS id, s.label AS src, e.rel AS rel, d.label AS dst,"
            "       e.source AS source, e.ts AS ts, e.status AS status,"
            "       e.source_type AS source_type, e.project AS project"
            " FROM edges e"
            " JOIN nodes s ON s.id = e.src"
            " JOIN nodes d ON d.id = e.dst"
            " WHERE COALESCE(e.status, 'approved') NOT IN (?, ?)"
            "   AND (LOWER(s.label) LIKE ? ESCAPE '\\' OR LOWER(d.label) LIKE ? ESCAPE '\\')"
        )
        params: List[Any] = list(_EXCLUDED_BY_DEFAULT) + [like, like]
        if project:
            sql += " AND COALESCE(e.project, '') = ?"
            params.append(project)
        sql += " ORDER BY e.id"

        out: List[Dict[str, Any]] = []
        for row in self.conn.execute(sql, params).fetchall():
            out.append(
                {
                    "id": int(row["id"]),
                    "src": row["src"],
                    "rel": row["rel"],
                    "dst": row["dst"],
                    "text": "%s %s %s" % (row["src"], row["rel"], row["dst"]),
                    "source": row["source"],
                    "ts": row["ts"],
                    "type": "experience",
                    "status": row["status"] or "approved",
                    "source_type": row["source_type"] or "conversation",
                    "project": row["project"],
                }
            )
            if len(out) >= max(0, int(top_k)):
                break
        return out

    def query(
        self,
        subject_or_keyword: str,
        top_k: Optional[int] = None,
        project: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Rank edges by keyword overlap with subject/relation/object labels.

        ``project`` (optional, additive) scopes the search to one project's
        experience; superseded/rejected edges are always excluded.
        """
        if top_k is None:
            top_k = int(self.rules["retrieval"]["graph_top_k"])
        probe_tokens = set(tokenize(subject_or_keyword))
        if not probe_tokens:
            return []

        results: List[Dict[str, Any]] = []
        sql = (
            "SELECT e.id AS id, s.label AS src, e.rel AS rel, d.label AS dst,"
            "       e.source AS source, e.ts AS ts, e.status AS status,"
            "       e.source_type AS source_type, e.project AS project"
            " FROM edges e"
            " JOIN nodes s ON s.id = e.src"
            " JOIN nodes d ON d.id = e.dst"
            " WHERE COALESCE(e.status, 'approved') NOT IN (?, ?)"
        )
        params: List[Any] = list(_EXCLUDED_BY_DEFAULT)
        if project:
            # This project's edges plus global (project-less) edges. Another
            # project's experience must never be ranked into this digest.
            sql += " AND (e.project = ? OR e.project IS NULL OR e.project = '')"
            params.append(project)
        rows = self.conn.execute(sql, params).fetchall()
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
                    "status": row["status"] or "approved",
                    "source_type": row["source_type"] or "conversation",
                    "project": row["project"],
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
