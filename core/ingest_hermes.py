"""Read-only ingestor for the Hermes session database (~/.hermes/state.db).

Exports real conversation turns as the JSONL format core.memory_curator already
consumes, so the same Curator -> Vector/Graph -> Context Builder pipeline runs
over the real corpus instead of the sample log.

READ-ONLY CONTRACT
------------------
The source database is opened through a ``file:...?mode=ro`` SQLite URI and is
never written, migrated or vacuumed by this module. ``mode=ro`` makes SQLite
itself reject any write attempt, so the guarantee does not depend on this code
being careful. We also avoid creating ``-wal``/``-shm`` side files by never
opening it read-write.

Run as::

    python3 -m core.ingest_hermes --out data/hermes_export.jsonl
    python3 -m core.ingest_hermes --db /tmp/fixture.db --since 2026-08-01 --limit 500
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

DEFAULT_STATE_DB = "~/.hermes/state.db"

# Tool-call internals are machine chatter, not memory worth curating.
DEFAULT_SKIP_ROLES = ("tool",)

REQUIRED_TABLES = ("messages", "sessions")


class HermesIngestError(RuntimeError):
    """Raised when the Hermes DB is missing, unreadable or not a Hermes DB."""


def resolve_db_path(state_db_path: Optional[str] = None) -> str:
    """Expand ``~`` and env vars; does not check existence."""
    raw = state_db_path or DEFAULT_STATE_DB
    return os.path.abspath(os.path.expanduser(os.path.expandvars(raw)))


def connect_readonly(state_db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open the Hermes state DB strictly read-only.

    Raises HermesIngestError with a clear message if the file is missing or is
    not a Hermes database - we never invent data.
    """
    path = resolve_db_path(state_db_path)
    if not os.path.exists(path):
        raise HermesIngestError(
            "Hermes state DB not found: %s (set --db or create a session first)" % path
        )
    if not os.path.isfile(path):
        raise HermesIngestError("Hermes state DB is not a regular file: %s" % path)

    uri = "file:%s?mode=ro" % _uri_quote(path)
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise HermesIngestError("cannot open %s read-only: %s" % (path, exc))
    conn.row_factory = sqlite3.Row

    try:
        present = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise HermesIngestError("%s is not a readable SQLite database: %s" % (path, exc))

    missing = [name for name in REQUIRED_TABLES if name not in present]
    if missing:
        conn.close()
        raise HermesIngestError(
            "%s does not look like a Hermes state DB (missing table(s): %s)"
            % (path, ", ".join(missing))
        )
    return conn


def _uri_quote(path: str) -> str:
    """Percent-encode the characters that are special inside a SQLite URI."""
    return path.replace("?", "%3f").replace("#", "%23")


def to_epoch(value: Any) -> Optional[float]:
    """Accept epoch seconds or an ISO-8601 string; return epoch seconds.

    Returns None for None/empty. Raises ValueError on an unparseable string so a
    typo in --since fails loudly instead of silently disabling the filter.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    iso = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError("cannot parse timestamp %r (use epoch seconds or ISO-8601)" % value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def to_iso(timestamp: Any) -> Optional[str]:
    """Unix epoch (REAL) -> ISO-8601 UTC string. Passes through usable strings."""
    if timestamp is None or timestamp == "":
        return None
    if isinstance(timestamp, str):
        try:
            timestamp = float(timestamp)
        except ValueError:
            return timestamp  # already an ISO-ish string; keep it verbatim
    try:
        moment = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalise_roles(include_roles: Optional[Iterable[str]]) -> Optional[frozenset]:
    if include_roles is None:
        return None
    roles = frozenset(str(r).strip().lower() for r in include_roles if str(r).strip())
    return roles or None


def iter_messages(
    state_db_path: Optional[str] = None,
    session_ids: Optional[Sequence[str]] = None,
    since_ts: Any = None,
    until_ts: Any = None,
    include_roles: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Iterator[Dict[str, Any]]:
    """Stream curator-shaped message dicts from the Hermes DB (read-only).

    Yields ``{role, content, ts, session_id, source}``. Tool rows and empty
    content are skipped unless ``include_roles`` explicitly asks for them.
    This is the streaming variant and reports no counters; use
    export_session_jsonl() when you need the skip/write summary.
    """
    for record, _kind in _iter_rows(
        state_db_path=state_db_path,
        session_ids=session_ids,
        since_ts=since_ts,
        until_ts=until_ts,
        include_roles=include_roles,
        limit=limit,
        conn=conn,
    ):
        if record is not None:
            yield record


def _iter_rows(
    state_db_path: Optional[str],
    session_ids: Optional[Sequence[str]],
    since_ts: Any,
    until_ts: Any,
    include_roles: Optional[Iterable[str]],
    limit: Optional[int],
    conn: Optional[sqlite3.Connection],
):
    """Shared row walker. Yields (record_or_None, kind) so callers can count.

    kind is one of: "written", "skipped_tool", "skipped_empty".
    """
    roles = _normalise_roles(include_roles)
    since = to_epoch(since_ts)
    until = to_epoch(until_ts)

    owns_conn = conn is None
    connection = conn if conn is not None else connect_readonly(state_db_path)

    where: List[str] = []
    params: List[Any] = []
    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        where.append("m.session_id IN (%s)" % placeholders)
        params.extend(list(session_ids))
    if since is not None:
        where.append("m.timestamp >= ?")
        params.append(since)
    if until is not None:
        where.append("m.timestamp <= ?")
        params.append(until)
    if roles:
        where.append("LOWER(COALESCE(m.role,'')) IN (%s)" % ",".join("?" for _ in roles))
        params.extend(sorted(roles))

    sql = (
        "SELECT m.id AS id, m.session_id AS session_id, m.role AS role,"
        "       m.content AS content, m.timestamp AS timestamp,"
        "       m.tool_name AS tool_name, s.source AS source, s.model AS model"
        "  FROM messages m"
        "  LEFT JOIN sessions s ON s.id = m.session_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY m.session_id, m.timestamp, m.id"
    if limit is not None and int(limit) > 0:
        sql += " LIMIT ?"
        params.append(int(limit))

    try:
        cursor = connection.execute(sql, params)
        for row in cursor:
            role = (row["role"] or "").strip().lower() or "unknown"

            if roles is None and role in DEFAULT_SKIP_ROLES:
                yield None, "skipped_tool"
                continue
            if roles is not None and role not in roles:
                # Defensive: SQL already filtered, but never trust one layer.
                yield None, "skipped_tool"
                continue

            content = _clean_content(row["content"])
            if not content:
                yield None, "skipped_empty"
                continue

            record = {
                "role": role,
                "content": content,
                "ts": to_iso(row["timestamp"]),
                "session_id": row["session_id"],
                "source": row["source"] or "hermes",
            }
            yield record, "written"
    finally:
        if owns_conn:
            connection.close()


def _clean_content(raw: Any) -> str:
    """Text only. Drops empty/whitespace and JSON tool-call payload noise."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raw = str(raw)
    text = raw.strip()
    if not text:
        return ""
    # Some clients store structured content as a JSON blob. Keep the text parts,
    # drop tool_use/tool_result/thinking entries.
    if text[0] in "[{":
        extracted = _text_from_structured(text)
        # _text_from_structured returns "" when the payload is recognised as a
        # structured (tool/thinking) envelope with no human text; we drop it.
        # It returns None only when the text is not valid JSON, in which case we
        # fall through to the raw string rather than silently discarding it.
        if extracted is not None:
            return extracted.strip()
    return text


def _text_from_structured(text: str) -> Optional[str]:
    """Pull human-readable text out of a JSON content payload.

    Returns the joined text parts, "" when the payload is a recognised
    tool/thinking envelope with no human text (so the caller drops it), or
    None when the shape is unrecognised or the text is not valid JSON (so the
    caller falls through to the raw string instead of silently dropping it).
    """
    try:
        payload = json.loads(text)
    except ValueError:
        return None

    # Walk states: 1 = found human text, 0 = recognised tool/thinking envelope
    # (no memory-worthy text), -1 = unrecognised shape.
    parts: List[str] = []

    def walk(node: Any) -> int:
        if isinstance(node, str):
            parts.append(node)
            return 1
        if isinstance(node, list):
            best = -1
            for item in node:
                result = walk(item)
                if result == 1:
                    return 1
                if result == 0:
                    best = 0
            return best
        if isinstance(node, dict):
            node_type = node.get("type")
            if node_type in ("tool_use", "tool_result", "thinking"):
                return 0
            if isinstance(node.get("text"), str):
                parts.append(node["text"])
                return 1
            for key in ("content", "message"):
                if key in node:
                    return walk(node[key])
            return -1
        return -1

    state = walk(payload)
    if state == 1:
        return "\n".join(p for p in parts if p and p.strip())
    if state == 0:
        return ""  # tool/thinking envelope: drop it
    return None  # unrecognised JSON or non-JSON: let caller keep raw text


def export_session_jsonl(
    state_db_path: Optional[str] = None,
    out_path: str = "data/hermes_export.jsonl",
    session_ids: Optional[Sequence[str]] = None,
    since_ts: Any = None,
    until_ts: Any = None,
    include_roles: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Export Hermes messages to a curator-ready JSONL file (source untouched).

    Returns {sessions, messages, written, skipped_tool, skipped_empty, out_path}.
    """
    conn = connect_readonly(state_db_path)
    try:
        total_messages = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

        out_dir = os.path.dirname(os.path.abspath(out_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        written = 0
        skipped_tool = 0
        skipped_empty = 0
        seen_sessions = set()

        with open(out_path, "w", encoding="utf-8") as handle:
            for record, kind in _iter_rows(
                state_db_path=None,
                session_ids=session_ids,
                since_ts=since_ts,
                until_ts=until_ts,
                include_roles=include_roles,
                limit=limit,
                conn=conn,
            ):
                if kind == "skipped_tool":
                    skipped_tool += 1
                    continue
                if kind == "skipped_empty":
                    skipped_empty += 1
                    continue
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
                if record["session_id"] is not None:
                    seen_sessions.add(record["session_id"])

        return {
            "sessions": len(seen_sessions),
            "messages": total_messages,
            "written": written,
            "skipped_tool": skipped_tool,
            "skipped_empty": skipped_empty,
            "out_path": out_path,
        }
    finally:
        conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export Hermes conversations to curator-ready JSONL (read-only)."
    )
    parser.add_argument("--db", default=DEFAULT_STATE_DB, help="path to Hermes state.db")
    parser.add_argument("--out", default="data/hermes_export.jsonl", help="output JSONL path")
    parser.add_argument("--session", action="append", dest="sessions",
                        help="only this session id (repeatable)")
    parser.add_argument("--since", default=None, help="epoch seconds or ISO-8601 lower bound")
    parser.add_argument("--until", default=None, help="epoch seconds or ISO-8601 upper bound")
    parser.add_argument("--roles", default=None,
                        help="comma-separated role allow-list (default: all but 'tool')")
    parser.add_argument("--limit", type=int, default=None, help="max rows to scan")
    args = parser.parse_args(argv)

    include_roles = args.roles.split(",") if args.roles else None
    try:
        summary = export_session_jsonl(
            state_db_path=args.db,
            out_path=args.out,
            session_ids=args.sessions,
            since_ts=args.since,
            until_ts=args.until,
            include_roles=include_roles,
            limit=args.limit,
        )
    except HermesIngestError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    print(json.dumps(summary, indent=2))
    if summary["written"] == 0:
        print("warning: no messages matched the filters", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
