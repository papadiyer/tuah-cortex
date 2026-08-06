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
        # Capture the store's embedding identity from existing rows so add()
        # and query() can enforce it without a full table scan per call. A
        # mixed store (pre-existing corruption) fails loud at open.
        self._store_identity = self._scan_identity()

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
            "SELECT id, text, source, ts, type, meta FROM knowledge ORDER BY id"
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def query(self, text: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return the top_k knowledge entries ranked by cosine similarity.

        Only rows whose full embedding identity (backend + model + dim) matches
        the active embedder are ranked. Rows from a different embedding space
        are skipped - they would score nonsense against the probe, so ranking
        them would silently pollute the result with unrelated memory.
        """
        if top_k is None:
            top_k = int(self.rules["retrieval"]["vector_top_k"])
        probe = self.embedder.embed(text)
        if not any(probe):
            return []
        active = self._active_identity()
        min_score = float(self.rules["retrieval"].get("min_score", 0.0))

        scored: List[Dict[str, Any]] = []
        for row in self.conn.execute(
            "SELECT id, text, embedding, source, ts, type, meta, embed_meta FROM knowledge"
        ):
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
