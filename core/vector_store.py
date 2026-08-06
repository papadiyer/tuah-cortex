"""SQLite-backed vector store for Knowledge entries.

Embeddings are computed locally (see core.rules.embed) and stored as a JSON
blob, so the database is portable and inspectable. Ranking is cosine similarity
computed in pure Python over the candidate rows.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.rules import cosine, embed, load_rules, repo_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    embedding  TEXT NOT NULL,
    source     TEXT,
    ts         TEXT,
    type       TEXT NOT NULL DEFAULT 'knowledge',
    meta       TEXT,
    fingerprint TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_knowledge_source ON knowledge(source);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class VectorStore:
    """Semantic long-term memory. Deterministic, local, no network."""

    def __init__(self, db_path: Optional[str] = None, rules: Optional[dict] = None):
        self.rules = rules or load_rules()
        if db_path is None:
            db_path = repo_path(self.rules["paths"]["vector_db"])
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
        vector = embed(text, self.rules)
        fingerprint = "%s::%s" % (source or "", text)
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO knowledge"
            " (text, embedding, source, ts, type, meta, fingerprint)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                text,
                json.dumps(vector),
                source,
                ts,
                entry_type,
                json.dumps(meta, sort_keys=True),
                fingerprint,
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
        probe = embed(text, self.rules)
        if not any(probe):
            return []
        min_score = float(self.rules["retrieval"].get("min_score", 0.0))

        scored: List[Dict[str, Any]] = []
        for row in self.conn.execute(
            "SELECT id, text, embedding, source, ts, type, meta FROM knowledge"
        ):
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
