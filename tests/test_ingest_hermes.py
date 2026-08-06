"""Hermes ingestor: read-only export of the real session schema to JSONL."""

import asyncio
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.graph_store import GraphStore  # noqa: E402
from core.ingest_hermes import (  # noqa: E402
    HermesIngestError,
    _clean_content,
    connect_readonly,
    export_session_jsonl,
    iter_messages,
    main,
    to_epoch,
    to_iso,
)
from core.memory_curator import Curator, read_log  # noqa: E402
from core.rules import load_rules  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402

# The real Hermes schema, verbatim from ~/.hermes/state.db.
MESSAGES_DDL = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT,
    role TEXT,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT
)
"""

SESSIONS_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER,
    tool_call_count INTEGER
)
"""

# 2026-08-01T00:00:00Z and onward.
T0 = 1785542400.0


def build_fixture_db(path):
    """Create a tiny DB with the real schema and a known mix of rows."""
    conn = sqlite3.connect(path)
    conn.executescript(MESSAGES_DDL + ";" + SESSIONS_DDL)
    conn.executemany(
        "INSERT INTO sessions (id, source, model, started_at, message_count)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            ("sess-a", "hermes-cli", "test-model", T0, 5),
            ("sess-b", "hermes-web", "test-model", T0 + 10000, 3),
        ],
    )
    rows = [
        # (session_id, role, content, tool_name, timestamp)
        ("sess-a", "user", "The memory_char_limit is 2200 and must never be exceeded.", None, T0 + 1),
        ("sess-a", "assistant", "core/vector_store.py imports core/rules.py for embeddings.", None, T0 + 2),
        ("sess-a", "tool", '{"stdout": "54 tests OK"}', "bash", T0 + 3),
        ("sess-a", "user", "   ", None, T0 + 4),
        ("sess-a", "USER", "Faisal is the final approval authority for any deploy.", None, T0 + 5),
        ("sess-b", "user", "Config rule: retrieval top_k stays at 5 until benchmarked.", None, T0 + 10001),
        ("sess-b", "assistant", "", None, T0 + 10002),
        ("sess-b", "tool", "internal tool chatter", "read_file", T0 + 10003),
    ]
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def read_jsonl(path):
    with open(path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class HermesFixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cortex-hermes-")
        self.db = os.path.join(self.tmp, "state.db")
        self.out = os.path.join(self.tmp, "export.jsonl")
        build_fixture_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestExport(HermesFixtureCase):
    def test_export_writes_valid_jsonl_and_summary(self):
        summary = export_session_jsonl(self.db, self.out)

        self.assertTrue(os.path.exists(self.out))
        records = read_jsonl(self.out)
        # 8 rows: 4 written, 2 tool, 2 empty/whitespace.
        self.assertEqual(summary["written"], 4)
        self.assertEqual(summary["skipped_tool"], 2)
        self.assertEqual(summary["skipped_empty"], 2)
        self.assertEqual(summary["messages"], 8)
        self.assertEqual(summary["sessions"], 2)
        self.assertEqual(summary["out_path"], self.out)
        self.assertEqual(len(records), 4)

    def test_record_shape_and_iso_timestamp(self):
        export_session_jsonl(self.db, self.out)
        record = read_jsonl(self.out)[0]
        self.assertEqual(
            sorted(record.keys()),
            ["content", "role", "session_id", "source", "ts"],
        )
        self.assertEqual(record["role"], "user")
        self.assertEqual(record["session_id"], "sess-a")
        self.assertEqual(record["source"], "hermes-cli")
        self.assertTrue(record["ts"].startswith("2026-08-"), record["ts"])
        self.assertTrue(record["ts"].endswith("Z"), record["ts"])

    def test_tool_rows_are_skipped_by_default(self):
        export_session_jsonl(self.db, self.out)
        roles = {r["role"] for r in read_jsonl(self.out)}
        self.assertNotIn("tool", roles)
        self.assertTrue(roles.issubset({"user", "assistant"}))

    def test_role_is_lowercased(self):
        export_session_jsonl(self.db, self.out)
        roles = [r["role"] for r in read_jsonl(self.out)]
        self.assertIn("user", roles)
        self.assertNotIn("USER", roles)

    def test_empty_content_is_skipped(self):
        export_session_jsonl(self.db, self.out)
        for record in read_jsonl(self.out):
            self.assertTrue(record["content"].strip())

    def test_include_roles_can_request_tool_rows(self):
        summary = export_session_jsonl(self.db, self.out, include_roles=["tool"])
        records = read_jsonl(self.out)
        self.assertEqual(summary["written"], 2)
        self.assertEqual({r["role"] for r in records}, {"tool"})

    def test_session_id_filter(self):
        summary = export_session_jsonl(self.db, self.out, session_ids=["sess-b"])
        records = read_jsonl(self.out)
        self.assertEqual(summary["written"], 1)
        self.assertEqual({r["session_id"] for r in records}, {"sess-b"})

    def test_since_ts_filter_epoch_and_iso_agree(self):
        by_epoch = export_session_jsonl(self.db, self.out, since_ts=T0 + 10000)
        self.assertEqual(by_epoch["written"], 1)

        iso_out = self.out + ".iso"
        by_iso = export_session_jsonl(self.db, iso_out, since_ts="2026-08-01T02:46:40Z")
        self.assertEqual(by_iso["written"], by_epoch["written"])

    def test_until_ts_filter(self):
        summary = export_session_jsonl(self.db, self.out, until_ts=T0 + 100)
        self.assertEqual(summary["written"], 3)

    def test_ordering_is_stable_by_session_then_time(self):
        export_session_jsonl(self.db, self.out)
        records = read_jsonl(self.out)
        keys = [(r["session_id"], r["ts"]) for r in records]
        self.assertEqual(keys, sorted(keys))

    def test_bad_timestamp_string_raises(self):
        with self.assertRaises(ValueError):
            export_session_jsonl(self.db, self.out, since_ts="not-a-date")


class TestReadOnlyContract(HermesFixtureCase):
    """The source DB must be provably unmodified by an export."""

    def test_source_db_bytes_unchanged(self):
        before = digest(self.db)
        before_mtime = os.stat(self.db).st_mtime_ns

        export_session_jsonl(self.db, self.out)

        self.assertEqual(digest(self.db), before, "source DB content changed")
        self.assertEqual(os.stat(self.db).st_mtime_ns, before_mtime, "source DB mtime changed")

    def test_sentinel_row_survives_and_row_count_stable(self):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES ('sess-a', 'user', 'SENTINEL do not delete', ?)",
            (T0 + 6,),
        )
        conn.commit()
        count_before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()

        export_session_jsonl(self.db, self.out)

        conn = sqlite3.connect(self.db)
        count_after = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        sentinel = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content LIKE 'SENTINEL%'"
        ).fetchone()[0]
        conn.close()

        self.assertEqual(count_after, count_before)
        self.assertEqual(sentinel, 1)

    def test_connection_rejects_writes(self):
        conn = connect_readonly(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM messages")
                conn.commit()
        finally:
            conn.close()

    def test_no_wal_or_shm_side_files_created(self):
        export_session_jsonl(self.db, self.out)
        self.assertFalse(os.path.exists(self.db + "-wal"))
        self.assertFalse(os.path.exists(self.db + "-shm"))


class TestDefensiveErrors(unittest.TestCase):
    def test_missing_db_raises_clear_error(self):
        with self.assertRaises(HermesIngestError) as ctx:
            export_session_jsonl("/nonexistent/path/state.db", "/tmp/x.jsonl")
        self.assertIn("not found", str(ctx.exception))

    def test_db_without_hermes_tables_raises(self):
        tmp = tempfile.mkdtemp(prefix="cortex-bad-")
        try:
            path = os.path.join(tmp, "other.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE unrelated (id INTEGER)")
            conn.commit()
            conn.close()
            with self.assertRaises(HermesIngestError) as ctx:
                export_session_jsonl(path, os.path.join(tmp, "o.jsonl"))
            self.assertIn("missing table", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_non_sqlite_file_raises(self):
        tmp = tempfile.mkdtemp(prefix="cortex-junk-")
        try:
            path = os.path.join(tmp, "junk.db")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("this is not a database")
            with self.assertRaises(HermesIngestError):
                export_session_jsonl(path, os.path.join(tmp, "o.jsonl"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestTimestampHelpers(unittest.TestCase):
    def test_to_epoch_accepts_epoch_iso_and_none(self):
        self.assertIsNone(to_epoch(None))
        self.assertIsNone(to_epoch(""))
        self.assertEqual(to_epoch(1785542400.0), 1785542400.0)
        self.assertEqual(to_epoch("1785542400"), 1785542400.0)
        self.assertEqual(to_epoch("2026-08-01T00:00:00Z"), 1785542400.0)

    def test_to_iso_roundtrip(self):
        self.assertEqual(to_iso(1785542400.0), "2026-08-01T00:00:00Z")
        self.assertIsNone(to_iso(None))


class TestIterMessages(HermesFixtureCase):
    def test_iter_messages_streams_same_records(self):
        streamed = list(iter_messages(self.db))
        export_session_jsonl(self.db, self.out)
        self.assertEqual(streamed, read_jsonl(self.out))

    def test_iter_messages_is_a_generator(self):
        import types

        self.assertIsInstance(iter_messages(self.db), types.GeneratorType)


class TestCli(HermesFixtureCase):
    def _run(self, argv):
        """Run the CLI with stdout/stderr captured so tests stay quiet."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_cli_exports_and_returns_zero(self):
        code, stdout, _ = self._run(["--db", self.db, "--out", self.out])
        self.assertEqual(code, 0)
        self.assertEqual(len(read_jsonl(self.out)), 4)
        self.assertEqual(json.loads(stdout)["written"], 4)

    def test_cli_missing_db_exits_two(self):
        code, _, stderr = self._run(["--db", "/nonexistent/state.db", "--out", self.out])
        self.assertEqual(code, 2)
        self.assertIn("not found", stderr)

    def test_cli_session_and_roles_filters(self):
        code, stdout, _ = self._run(
            ["--db", self.db, "--out", self.out, "--session", "sess-b", "--roles", "user"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["written"], 1)


class TestCuratorChain(HermesFixtureCase):
    """Acceptance: the exported JSONL is directly ingestable by the curator."""

    def test_export_then_curate(self):
        export_session_jsonl(self.db, self.out)

        messages, malformed = read_log(self.out)
        self.assertEqual(malformed, 0)
        self.assertEqual(len(messages), 4)

        rules = load_rules()
        vector = VectorStore(":memory:", rules=rules)
        graph = GraphStore(":memory:", rules=rules)
        curator = Curator(rules=rules, vector_store=vector, graph_store=graph)
        try:
            report = asyncio.run(curator.ingest(self.out))
        finally:
            vector.close()
            graph.close()

        self.assertEqual(report["messages"], 4)
        self.assertEqual(report["malformed_lines"], 0)
        self.assertGreater(report["segments"], 0)
        self.assertGreater(
            report["knowledge_added"] + report["experience_edges"] + report["experience_nodes"],
            0,
            "curator stored nothing from the Hermes export",
        )


class TestToolOnlyPayloadDropped(unittest.TestCase):
    """Regression for P1: a tool-only structured payload must NOT be kept as
    memory. Faisal's reproducer returned the raw JSON (with 'thinking' content)
    verbatim; that leaks machine chatter into the corpus.
    """

    def test_tool_result_with_thinking_is_dropped(self):
        payload = json.dumps(
            {
                "type": "tool_result",
                "content": [{"type": "thinking", "text": "secret internal reasoning"}],
            }
        )
        self.assertEqual(_clean_content(payload), "")

    def test_tool_use_envelope_is_dropped(self):
        payload = json.dumps(
            {"type": "tool_use", "name": "read_file", "input": {"path": "/etc/passwd"}}
        )
        self.assertEqual(_clean_content(payload), "")

    def test_list_of_tool_results_is_dropped(self):
        payload = json.dumps(
            [
                {"type": "tool_result", "content": [{"type": "thinking", "text": "x"}]},
                {"type": "tool_use", "name": "y", "input": {}},
            ]
        )
        self.assertEqual(_clean_content(payload), "")

    def test_thinking_block_alone_is_dropped(self):
        self.assertEqual(_clean_content(json.dumps({"type": "thinking", "text": "nope"})), "")

    def test_human_text_in_structured_is_kept(self):
        payload = json.dumps({"type": "text", "text": "Faisal prefers short answers"})
        self.assertEqual(_clean_content(payload), "Faisal prefers short answers")

    def test_non_json_bracket_string_is_kept_verbatim(self):
        # Not valid JSON - must NOT be dropped (only genuine tool envelopes are).
        self.assertEqual(_clean_content("[just a plain note]"), "[just a plain note]")


if __name__ == "__main__":
    unittest.main()
