"""SQLite-backed vector store for Knowledge entries.

Embeddings come from a pluggable Embedder (see core.rules.get_embedder) and are
stored as a JSON blob, so the database is portable and inspectable. Ranking is
cosine similarity computed in pure Python over the candidate rows.

The store never calls a specific embedding function directly: pass ``embedder=``
to swap backends. Mixing vectors from two different backends in one database
would make cosine scores meaningless, so use a separate db per backend.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.rules import Embedder, cosine, get_embedder, load_rules, repo_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    embedding  TEXT NOT NULL,
    source     TEXT,
    ts         TEXT,
    type       TEXT NOT NULL DEFAULT 'knowledge',
    meta       TEXT,
    fingerprint TEXT UNIQUE,
    embed_meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_knowledge_source ON knowledge(source);
"""

# Additive provenance/status columns (MEMORY_SCHEMA.md section 1). Older
# databases predate them, so they are added by migration rather than being
# required at open time.
_ADDITIVE_COLUMNS = (
    ("embed_meta", "TEXT"),
    ("status", "TEXT"),
    ("source_type", "TEXT"),
    ("source_id", "TEXT"),
    ("project", "TEXT"),
    ("confidence", "REAL"),
    ("updated_at", "TEXT"),
)

# Default retrieval excludes memories that were replaced or rejected; they stay
# queryable for provenance but must not surface as current truth.
_EXCLUDED_BY_DEFAULT = ("superseded", "rejected")


def _ensure_columns(conn: "sqlite3.Connection") -> None:
    """Add additive columns to an existing knowledge table if absent.

    Old databases created before provenance/status tagging lack these columns;
    we add them rather than refusing to open the database. Rows written before
    tagging carry NULL and are treated as 'unknown' by the compatibility check
    and as status='approved' by retrieval (they predate the status model).
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(knowledge)")}
    changed = False
    for name, coltype in _ADDITIVE_COLUMNS:
        if name not in cols:
            conn.execute("ALTER TABLE knowledge ADD COLUMN %s %s" % (name, coltype))
            changed = True
    if changed:
        conn.commit()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class VectorStore:
    """Semantic long-term memory. Deterministic, local, no network."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        rules: Optional[dict] = None,
        embedder: Optional[Embedder] = None,
    ):
        # Set before anything that can fail so close()/__del__ stay safe even
        # if construction aborts part-way (e.g. a bad path or a mixed store).
        self._closed = False
        self.conn = None  # type: ignore[assignment]
        self.rules = rules or load_rules()
        self.embedder = embedder if embedder is not None else get_embedder(self.rules)
        if db_path is None:
            db_path = repo_path(self.rules["paths"]["vector_db"])
        self.db_path = db_path
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        # check_same_thread=False: the threaded HTTP server hands each request
        # to a worker thread, so the connection outlives the thread that made
        # it. Callers that share a store across threads MUST serialise access
        # (api.app.CortexApp holds a lock around dispatch); SQLite itself is
        # not safe for concurrent use of one connection.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        _ensure_columns(self.conn)
        self.conn.commit()
        # Capture the store's embedding identity from existing rows so add()
        # and query() can enforce it without a full table scan per call. A
        # mixed store (pre-existing corruption) fails loud at open.
        try:
            self._store_identity = self._scan_identity()
        except Exception:
            # The store is unusable, but the connection is already open. Close
            # it before propagating, or a caller that correctly handles the
            # ValueError still leaks the handle it never got a reference to.
            self.close()
            raise

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """Close the SQLite connection. Idempotent and safe to call twice.

        The connection object is deliberately kept (not set to None) so that
        using a closed store raises sqlite3.ProgrammingError - a clear "you
        used this after closing it" - rather than AttributeError.

        Tolerates a partially-constructed instance (``__init__`` raised before
        the attributes existed), so error paths can always close defensively.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True
        conn = getattr(self, "conn", None)
        if conn is not None:
            conn.close()

    @property
    def closed(self) -> bool:
        """True once close() has run. Lets callers audit lifecycle state."""
        return self._closed

    def __del__(self) -> None:  # pragma: no cover - GC timing dependent
        """Best-effort safety net: never raise, never mask a real leak.

        This exists so a store dropped without close() does not emit a
        ResourceWarning at GC time. It is a backstop, NOT the contract -
        callers must still close explicitly (or use the context manager).
        """
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "VectorStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- writes ------------------------------------------------------------
    def add(self, text: str, meta: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """Insert a knowledge entry. Returns row id, or None if a duplicate.

        Duplicates are detected by an exact-text fingerprint so re-running the
        curator on the same log does not inflate the store.
        """
        text = (text or "").strip()
        if not text:
            return None
        meta = dict(meta or {})
        source = meta.pop("source", None)
        ts = meta.pop("ts", None) or _utc_now()
        entry_type = meta.pop("type", "knowledge")
        # Provenance/status (MEMORY_SCHEMA.md section 1). Promoted out of the
        # meta blob into real columns so retrieval can filter on them in SQL.
        # Anything not supplied keeps the pre-provenance default so existing
        # callers (the curator's two-key meta) behave exactly as before.
        status = meta.pop("status", None) or "approved"
        source_type = meta.pop("source_type", None) or "conversation"
        source_id = meta.pop("source_id", None) or source
        project = meta.pop("project", None)
        confidence = meta.pop("confidence", None)
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        vector = self.embedder.embed(text)
        row_identity = {
            "backend": self.embedder.name,
            "model": self.embedder.model,
            "dim": len(vector),
        }
        # Refuse to mix incompatible embeddings at the API boundary, not just
        # via the curator. The first write defines the store's identity; any
        # later write with a different backend/model/dim is rejected loudly.
        if self._store_identity is not None and row_identity != self._store_identity:
            raise ValueError(
                "cannot add embedding with identity %s to vector store %s that "
                "already holds identity %s"
                % (row_identity, self.db_path, self._store_identity)
            )
        tag = json.dumps(row_identity, sort_keys=True)
        fingerprint = "%s::%s" % (source or "", text)
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO knowledge"
            " (text, embedding, source, ts, type, meta, fingerprint, embed_meta,"
            "  status, source_type, source_id, project, confidence, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                text,
                json.dumps(vector),
                source,
                ts,
                entry_type,
                json.dumps(meta, sort_keys=True),
                fingerprint,
                tag,
                status,
                source_type,
                source_id,
                project,
                confidence,
                _utc_now(),
            ),
        )
        self.conn.commit()
        if cursor.rowcount == 0:
            return None
        self._store_identity = row_identity
        return int(cursor.lastrowid)

    def add_many(self, items: List[Dict[str, Any]]) -> int:
        """Bulk add. Each item: {"text": str, ...meta}. Returns inserted count."""
        inserted = 0
        for item in items:
            payload = dict(item)
            text = payload.pop("text", "")
            if self.add(text, payload) is not None:
                inserted += 1
        return inserted

    # -- reads -------------------------------------------------------------
    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0])

    def all_entries(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, text, source, ts, type, meta, status, source_type,"
            "       source_id, project, confidence"
            "  FROM knowledge ORDER BY id"
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_by_id(
        self, memory_id: int, expected_type: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch exactly one memory row by primary key, or None.

        The canonical id-indexed read. Unlike ``search``, this has NO limit and
        no default status filter: it looks the row up directly by primary key,
        so a row is found whether it is the newest in the store or the oldest,
        and whatever its status. Scanning ``search(..., limit=N)`` for an id was
        the P2-MEDIUM bug - past N rows the target simply fell off the window
        and an existing decision was reported 404.

        ``expected_type`` narrows the lookup in SQL (e.g. "decision"), so a
        wrong-type id returns None rather than a row the caller must re-check.
        """
        try:
            key = int(memory_id)
        except (TypeError, ValueError):
            return None

        sql = (
            "SELECT id, text, source, ts, type, meta, status, source_type,"
            "       source_id, project, confidence"
            "  FROM knowledge WHERE id = ?"
        )
        params: List[Any] = [key]
        if expected_type:
            sql += " AND type = ?"
            params.append(str(expected_type))

        row = self.conn.execute(sql, params).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def search(
        self,
        query: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Filtered memory search (API_CONTRACTS.md section 4).

        Structured filters are applied in SQL; ``query`` (when supplied) then
        ranks the survivors by cosine similarity. Without a query the rows are
        returned newest-first, so a pure filter search (e.g. "all approved
        decisions for project X") does not depend on the embedder at all.
        """
        filters = dict(filters or {})
        where: List[str] = []
        params: List[Any] = []

        def _add(clause: str, value: Any) -> None:
            where.append(clause)
            params.append(value)

        if filters.get("project"):
            _add("project = ?", filters["project"])
        if filters.get("memory_type"):
            _add("type = ?", filters["memory_type"])
        if filters.get("source"):
            _add("source = ?", filters["source"])
        if filters.get("source_type"):
            _add("source_type = ?", filters["source_type"])
        if filters.get("status"):
            _add("COALESCE(status, 'approved') = ?", filters["status"])
        elif filters.get("approved_only"):
            _add("COALESCE(status, 'approved') = ?", "approved")
        else:
            where.append(
                "COALESCE(status, 'approved') NOT IN ('%s')" % "','".join(_EXCLUDED_BY_DEFAULT)
            )
        if filters.get("min_confidence") is not None:
            _add("COALESCE(confidence, 1.0) >= ?", float(filters["min_confidence"]))
        if filters.get("date_from"):
            _add("COALESCE(ts, '') >= ?", str(filters["date_from"]))
        if filters.get("date_to"):
            _add("COALESCE(ts, '') <= ?", str(filters["date_to"]))
        if filters.get("entity"):
            # Escape LIKE metacharacters, or entity='%' would match the whole
            # store instead of searching for a literal percent sign.
            literal = (
                str(filters["entity"])
                .lower()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            where.append("LOWER(text) LIKE ? ESCAPE '\\'")
            params.append("%%%s%%" % literal)

        sql = (
            "SELECT id, text, embedding, source, ts, type, meta, embed_meta,"
            "       status, source_type, source_id, project, confidence"
            "  FROM knowledge"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(ts, '') DESC, id DESC"

        rows = self.conn.execute(sql, params).fetchall()

        probe = self.embedder.embed(query) if query else None
        active = self._active_identity()
        results: List[Dict[str, Any]] = []
        for row in rows:
            entry = self._row_to_dict(row)
            if probe is not None and any(probe):
                # Only rank rows from the same embedding space; a mismatched row
                # would score nonsense. It is still returned (filters matched),
                # just without a misleading similarity score.
                if self._row_identity(row["embed_meta"]) != active:
                    entry["score"] = None
                else:
                    try:
                        entry["score"] = round(cosine(probe, json.loads(row["embedding"])), 6)
                    except (TypeError, ValueError):
                        entry["score"] = None
            results.append(entry)

        if probe is not None and any(probe):
            results.sort(key=lambda e: (e["score"] is None, -(e["score"] or 0.0), e["id"]))
        return results[: max(0, int(limit))]

    def set_status(self, memory_id: int, status: str) -> bool:
        """Update a memory's status. Never hard-deletes (MEMORY_SCHEMA section 3)."""
        cursor = self.conn.execute(
            "UPDATE knowledge SET status = ?, updated_at = ? WHERE id = ?",
            (status, _utc_now(), int(memory_id)),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def query(
        self,
        text: str,
        top_k: Optional[int] = None,
        project: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return the top_k knowledge entries ranked by cosine similarity.

        Only rows whose full embedding identity (backend + model + dim) matches
        the active embedder are ranked. Rows from a different embedding space
        are skipped - they would score nonsense against the probe, so ranking
        them would silently pollute the result with unrelated memory.

        ``project`` (optional, additive) scopes retrieval to one project plus
        project-less rows. Rows tagged with a *different* project are excluded:
        another client's memory must never be ranked into this project's digest.
        Project-less rows are kept because they are global memory (preferences,
        operating rules) that legitimately applies everywhere.
        """
        if top_k is None:
            top_k = int(self.rules["retrieval"]["vector_top_k"])
        probe = self.embedder.embed(text)
        if not any(probe):
            return []
        active = self._active_identity()
        min_score = float(self.rules["retrieval"].get("min_score", 0.0))

        sql = (
            "SELECT id, text, embedding, source, ts, type, meta, embed_meta,"
            "       status, source_type, source_id, project, confidence"
            "  FROM knowledge"
            " WHERE COALESCE(status, 'approved') NOT IN (?, ?)"
        )
        params: List[Any] = list(_EXCLUDED_BY_DEFAULT)
        if project:
            # This project's rows plus global (project-less) rows only.
            sql += " AND (project = ? OR project IS NULL OR project = '')"
            params.append(project)

        scored: List[Dict[str, Any]] = []
        for row in self.conn.execute(sql, params):
            identity = self._row_identity(row["embed_meta"])
            if identity != active:
                # Different backend / model / dimension: not comparable. Skip
                # explicitly rather than letting cosine return a misleading
                # score. The store identity is reported via check_compatibility().
                continue
            try:
                vector = json.loads(row["embedding"])
            except (TypeError, ValueError):
                continue
            score = cosine(probe, vector)
            if score < min_score:
                continue
            entry = self._row_to_dict(row)
            entry["score"] = round(score, 6)
            scored.append(entry)

        scored.sort(key=lambda e: (-e["score"], e["id"]))
        return scored[:top_k]

    @staticmethod
    def _row_identity(raw_meta: Any) -> Dict[str, Any]:
        """Normalise a stored embed_meta blob into an identity dict.

        Unknown/missing metadata is treated as a fully-unknown identity so it
        never silently matches the active embedder.
        """
        if not raw_meta:
            return {"backend": None, "model": None, "dim": None}
        try:
            tag = json.loads(raw_meta)
        except (TypeError, ValueError):
            return {"backend": None, "model": None, "dim": None}
        return {
            "backend": tag.get("backend"),
            "model": tag.get("model"),
            "dim": int(tag.get("dim", -1)),
        }

    def _active_identity(self) -> Dict[str, Any]:
        return {
            "backend": self.embedder.name,
            "model": self.embedder.model,
            "dim": self.embedder.dimensions,
        }

    def _scan_identity(self) -> Optional[Dict[str, Any]]:
        """Identity of existing rows, or None if the store is empty.

        Raises ValueError if the store already holds rows from more than one
        embedding identity - that is pre-existing corruption and must not be
        trusted or silently "fixed" by a later write.
        """
        seen: Optional[Dict[str, Any]] = None
        for (raw,) in self.conn.execute("SELECT embed_meta FROM knowledge"):
            ident = self._row_identity(raw)
            if seen is None:
                seen = ident
            elif ident != seen:
                raise ValueError(
                    "vector store %s already holds mixed embedding identities "
                    "(%s vs %s); use a fresh database" % (self.db_path, seen, ident)
                )
        return seen

    def check_compatibility(self, raise_on_mismatch: bool = False) -> Dict[str, Any]:
        """Report whether existing rows match the active embedder identity.

        Returns {total, matched, mismatched, active_backend, active_model,
        active_dim}. With ``raise_on_mismatch=True``, raises ValueError when any
        row's stored backend/model/dimension differs from the active embedder -
        this is how the curator/CLI catches a backend or model flip (e.g.
        deterministic -> sentence-transformers, or one ST model for another
        with the same dimension) before it silently mixes incompatible vectors.
        """
        active = self._active_identity()
        total = 0
        matched = 0
        mismatched = 0
        for row in self.conn.execute("SELECT embed_meta FROM knowledge"):
            total += 1
            if self._row_identity(row["embed_meta"]) == active:
                matched += 1
            else:
                mismatched += 1
        if raise_on_mismatch and mismatched:
            raise ValueError(
                "vector store %s holds %d row(s) from a different embedding "
                "identity than the active embedder (backend=%s model=%s dim=%d). "
                "Use a fresh database or switch back the embedding config."
                % (self.db_path, mismatched, active["backend"], active["model"], active["dim"])
            )
        return {
            "total": total,
            "matched": matched,
            "mismatched": mismatched,
            "active_backend": active["backend"],
            "active_model": active["model"],
            "active_dim": active["dim"],
        }

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        keys = row.keys()
        meta_raw = row["meta"] if "meta" in keys else None
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except ValueError:
            meta = {}
        entry = {
            "id": int(row["id"]),
            "text": row["text"],
            "source": row["source"] if "source" in keys else None,
            "ts": row["ts"] if "ts" in keys else None,
            "type": row["type"] if "type" in keys else "knowledge",
            "meta": meta,
        }
        # Surface provenance/status alongside the legacy shape. Rows predating
        # the migration report status='approved' (they were written before the
        # status model existed) rather than an alarming NULL.
        if "status" in keys:
            entry["status"] = row["status"] or "approved"
        if "source_type" in keys:
            entry["source_type"] = row["source_type"] or "conversation"
        if "source_id" in keys:
            entry["source_id"] = row["source_id"]
        if "project" in keys:
            entry["project"] = row["project"]
        if "confidence" in keys:
            entry["confidence"] = row["confidence"]
        return entry


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    import sys

    store = VectorStore()
    if len(sys.argv) > 1:
        for hit in store.query(" ".join(sys.argv[1:])):
            print("%.4f  %s" % (hit["score"], hit["text"][:120]))
    else:
        print("knowledge entries: %d" % store.count())
    store.close()
