"""Shared test lifecycle helpers.

Every store in this codebase owns a SQLite connection. A test that constructs
one and drops it without closing leaves the connection to be reclaimed at
garbage-collection time, which is what made the suite emit ResourceWarning.

``closing()`` binds an object's lifetime to the test that created it, so the
close cannot be forgotten and cannot be skipped by an early assertion failure
(``addCleanup`` runs even when the test raises, unlike code at the end of the
test body or a tearDown that asserts first).

Usage::

    from tests.support import closing

    store = closing(self, VectorStore(":memory:"))
"""

from __future__ import annotations

from typing import Any, TypeVar
import unittest

T = TypeVar("T")


def closing(testcase: unittest.TestCase, obj: T) -> T:
    """Register ``obj.close()`` as test cleanup and return ``obj``.

    Cleanups run in reverse order of registration, so a wrapper registered
    after the stores it wraps is torn down first - the natural order.
    """
    close = getattr(obj, "close", None)
    if not callable(close):
        raise TypeError("closing() expects an object with a close() method, got %r" % type(obj))
    testcase.addCleanup(close)
    return obj


def assert_closed(testcase: unittest.TestCase, store: Any, label: str = "store") -> None:
    """Assert a store's SQLite connection is genuinely closed.

    Checks observable behaviour (the connection refuses to execute) rather
    than trusting a bookkeeping flag, so a close() that sets the flag but
    leaks the handle still fails the assertion.
    """
    import sqlite3

    conn = getattr(store, "conn", None)
    testcase.assertIsNotNone(conn, "%s has no connection attribute" % label)
    with testcase.assertRaises(sqlite3.ProgrammingError, msg="%s is still open" % label):
        conn.execute("SELECT 1")
