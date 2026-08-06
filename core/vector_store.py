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


def _ensure_embed_meta(conn: "sqlite3.Connection") -> None:
    """Add the embed_meta column to an existing knowledge table if absent.

    Old databases created before backend tagging lack the column; we add it
    rather than refusing to open them. Rows written before tagging carry
    NULL embed_meta and are treated as 'unknown backend' by the compatibility
    check.
    """
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(knowledge)")
    }
    if "embed_meta" not in cols:
        conn.execute("ALTER TABLE knowledge ADD COLUMN embed_meta TEXT")
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
        self.rules = rules or load_rules()
        self.embedder = embedder if embedder is not None else get_embedder(self.rules)
        if db_path is None:
            db_path = repo_path(self.rules["paths"]["vector_db"])
        self.db_path = db_path
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        _ensure_embed_meta(self.conn)
        self.conn.commit()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

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
        vector = self.embedder.embed(text)
        tag = json.dumps(
            {"backend": self.embedder.name, "dim": len(vector)}, sort_keys=True
        )
        fingerprint = "%s::%s" % (source or "", text)
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO knowledge"
            " (text, embedding, source, ts, type, meta, fingerprint, embed_meta)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                text,
                json.dumps(vector),
                source,
                ts,
                entry_type,
                json.dumps(meta, sort_keys=True),
                fingerprint,
                tag,
            ),
        )
        self.conn.commit()
        if cursor.rowcount == 0:
            return None
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
            "SELECT id, text, source, ts, type, meta FROM knowledge ORDER BY id"
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def query(self, text: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return the top_k knowledge entries ranked by cosine similarity."""
        if top_k is None:
            top_k = int(self.rules["retrieval"]["vector_top_k"])
        probe = self.embedder.embed(text)
        if not any(probe):
            return []
        min_score = float(self.rules["retrieval"].get("min_score", 0.0))

        scored: List[Dict[str, Any]] = []
        for row in self.conn.execute(
            "SELECT id, text, embedding, source, ts, type, meta, embed_meta FROM knowledge"
        ):
            try:
                vector = json.loads(row["embedding"])
            except (TypeError, ValueError):
                continue
            # Skip rows whose embedding dimension does not match the active
            # backend. A different-dim vector would score 0.0 against the probe
            # (cosine returns 0 on length mismatch) and be silently dropped -
            # old memory would go unranked with no error. We skip it explicitly
            # and keep the row visible via check_compatibility() so the caller
            # can fail loudly instead of trusting a partially-ranked result.
            if len(vector) != len(probe):
                continue
            score = cosine(probe, vector)
            if score < min_score:
                continue
            entry = self._row_to_dict(row)
            entry["score"] = round(score, 6)
            scored.append(entry)

        scored.sort(key=lambda e: (-e["score"], e["id"]))
        return scored[:top_k]

    def check_compatibility(self, raise_on_mismatch: bool = False) -> Dict[str, Any]:
        """Report whether existing rows match the active embedder.

        Returns {total, matched, mismatched, active_backend, active_dim}.
        With ``raise_on_mismatch=True``, raises ValueError when any row's
        stored backend/dimension differs from the active embedder - this is
        how the curator/CLI catches a backend flip (e.g. deterministic ->
        sentence-transformers) before it silently mixes incompatible vectors.
        """
        active_backend = self.embedder.name
        active_dim = self.embedder.dimensions
        total = 0
        matched = 0
        mismatched = 0
        for row in self.conn.execute("SELECT embed_meta FROM knowledge"):
            total += 1
            raw = row["embed_meta"]
            if not raw:
                # Pre-tag row: unknown backend. Treat as a mismatch so a backend
                # flip is caught rather than trusted.
                mismatched += 1
                continue
            try:
                tag = json.loads(raw)
            except (TypeError, ValueError):
                mismatched += 1
                continue
            if tag.get("backend") == active_backend and int(tag.get("dim", -1)) == active_dim:
                matched += 1
            else:
                mismatched += 1
        if raise_on_mismatch and mismatched:
            raise ValueError(
                "vector store %s holds %d row(s) from a different embedding "
                "backend/dimension than the active embedder (backend=%s dim=%d). "
                "Use a fresh database or switch back the embedding.backend config."
                % (self.db_path, mismatched, active_backend, active_dim)
            )
        return {
            "total": total,
            "matched": matched,
            "mismatched": mismatched,
            "active_backend": active_backend,
            "active_dim": active_dim,
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
        return {
            "id": int(row["id"]),
            "text": row["text"],
            "source": row["source"] if "source" in keys else None,
            "ts": row["ts"] if "ts" in keys else None,
            "type": row["type"] if "type" in keys else "knowledge",
            "meta": meta,
        }


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    import sys

    store = VectorStore()
    if len(sys.argv) > 1:
        for hit in store.query(" ".join(sys.argv[1:])):
            print("%.4f  %s" % (hit["score"], hit["text"][:120]))
    else:
        print("knowledge entries: %d" % store.count())
    store.close()
