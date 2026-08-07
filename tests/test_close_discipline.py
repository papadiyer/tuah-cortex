"""Lifecycle/close discipline (GA hardening, Task 6).

The suite used to emit ResourceWarning because stores opened a SQLite
connection that nothing ever closed; the handle was reclaimed at
garbage-collection time instead. These tests pin the contract that replaced
that behaviour:

  1. every store closes idempotently and observably;
  2. an owner closes exactly the handles it opened - no more (closing a
     borrowed store would kill a running service) and no fewer (dropping one
     is the leak);
  3. ownership is auditable via ``own_stores`` before close() is called.

The zero-ResourceWarning gate itself is enforced by running the whole suite
under ``-W error::ResourceWarning``; these tests explain *why* it holds, and
fail loudly if someone re-introduces an unclosed handle.
"""

import copy
import gc
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.service import CortexService  # noqa: E402
from core.context_builder import ContextBuilder  # noqa: E402
from core.graph_store import GraphStore  # noqa: E402
from core.memory_curator import Curator  # noqa: E402
from core.rules import load_rules  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402
from tests.support import assert_closed, closing  # noqa: E402
from workers.queue import EventQueue  # noqa: E402


def _is_open(store):
    """True when the store's connection still accepts a statement."""
    try:
        store.conn.execute("SELECT 1")
        return True
    except sqlite3.ProgrammingError:
        return False


class _IsolatedStoreTest(unittest.TestCase):
    """Base class: rules whose store paths point into a temp directory.

    Some cases below deliberately construct a store-less ContextBuilder or
    Curator to prove it opens - and then closes - its own handles. With the
    default rules that would open the real production databases in data/, so
    the paths are redirected per test.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cortex-close-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rules = copy.deepcopy(load_rules())
        for key, name in (
            ("vector_db", "vector.db"),
            ("graph_db", "graph.db"),
            ("queue_db", "queue.db"),
        ):
            # Absolute paths: repo_path() leaves an absolute path untouched.
            self.rules["paths"][key] = os.path.join(self.tmp, name)

    def path(self, name):
        return os.path.join(self.tmp, name)


class TestStoreCloseIsIdempotentAndObservable(_IsolatedStoreTest):
    """Each store: close() releases the handle, twice is not an error."""

    def _stores(self):
        return [
            ("VectorStore", VectorStore(":memory:")),
            ("GraphStore", GraphStore(":memory:")),
            ("EventQueue", EventQueue(self.path("q.db"))),
        ]

    def test_close_releases_the_connection(self):
        for label, store in self._stores():
            with self.subTest(store=label):
                self.assertTrue(_is_open(store), "%s should start open" % label)
                store.close()
                assert_closed(self, store, label)
                self.assertTrue(store.closed, "%s.closed should be True" % label)

    def test_close_is_idempotent(self):
        for label, store in self._stores():
            with self.subTest(store=label):
                store.close()
                store.close()  # must not raise
                store.close()
                assert_closed(self, store, label)

    def test_context_manager_closes_on_exit(self):
        with VectorStore(":memory:") as store:
            self.assertTrue(_is_open(store))
        assert_closed(self, store, "VectorStore")

        with GraphStore(":memory:") as store:
            self.assertTrue(_is_open(store))
        assert_closed(self, store, "GraphStore")

    def test_context_manager_closes_even_when_body_raises(self):
        store = None
        with self.assertRaises(RuntimeError):
            with VectorStore(":memory:") as store:
                raise RuntimeError("boom")
        assert_closed(self, store, "VectorStore")

    def test_use_after_close_fails_loudly(self):
        """A closed store must not silently appear to work."""
        store = VectorStore(":memory:")
        store.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            store.add("should not be written", {"source": "t"})


class TestContextBuilderOwnership(_IsolatedStoreTest):
    """Task 6 #2: the builder tracks vector/graph ownership separately."""

    def test_close_closes_both_stores_it_created(self):
        builder = ContextBuilder(rules=self.rules, use_ripgrep=False)
        vector, graph = builder.vector, builder.graph

        self.assertEqual(
            set(builder.own_stores), {"vector_store", "graph_store"},
            "a builder that opened both stores must report owning both",
        )
        self.assertTrue(_is_open(vector))
        self.assertTrue(_is_open(graph))

        builder.close()

        assert_closed(self, vector, "vector store")
        assert_closed(self, graph, "graph store")

    def test_injected_stores_are_not_owned_and_survive_close(self):
        """CortexService shares one pair of stores across every request.

        It builds a short-lived ContextBuilder per request and closes it each
        time, so closing an injected store would tear down the live service
        after a single call.
        """
        vector = closing(self, VectorStore(":memory:"))
        graph = closing(self, GraphStore(":memory:"))
        builder = ContextBuilder(vector_store=vector, graph_store=graph, use_ripgrep=False)

        self.assertEqual(builder.own_stores, {}, "injected stores must not be owned")

        builder.close()

        self.assertTrue(_is_open(vector), "an injected vector store must stay open")
        self.assertTrue(_is_open(graph), "an injected graph store must stay open")

    def test_partial_injection_still_closes_the_created_store(self):
        """Regression: the old all-or-nothing flag leaked here.

        ``_owns_stores = vector_store is None and graph_store is None`` was
        False whenever ONE store was injected, so the store the builder
        constructed itself was never closed - it was collected at GC time and
        raised ResourceWarning.
        """
        injected = closing(self, VectorStore(":memory:"))
        builder = ContextBuilder(rules=self.rules, vector_store=injected, use_ripgrep=False)
        created_graph = builder.graph

        self.assertEqual(
            set(builder.own_stores), {"graph_store"},
            "only the self-constructed graph store is owned",
        )

        builder.close()

        assert_closed(self, created_graph, "self-constructed graph store")
        self.assertTrue(_is_open(injected), "the injected vector store must stay open")

    def test_close_is_idempotent(self):
        builder = ContextBuilder(rules=self.rules, use_ripgrep=False)
        builder.close()
        builder.close()  # must not raise on an already-closed connection


class TestCuratorOwnership(_IsolatedStoreTest):
    """The curator had the same all-or-nothing ownership bug."""

    def test_partial_injection_still_closes_the_created_store(self):
        injected = closing(self, VectorStore(":memory:"))
        curator = Curator(rules=self.rules, vector_store=injected)
        created_graph = curator.graph

        self.assertEqual(set(curator.own_stores), {"graph_store"})

        curator.close()

        assert_closed(self, created_graph, "self-constructed graph store")
        self.assertTrue(_is_open(injected), "the injected vector store must stay open")

    def test_injected_stores_survive_close(self):
        vector = closing(self, VectorStore(":memory:"))
        graph = closing(self, GraphStore(":memory:"))
        curator = Curator(rules=self.rules, vector_store=vector, graph_store=graph)

        self.assertEqual(curator.own_stores, {})
        curator.close()

        self.assertTrue(_is_open(vector))
        self.assertTrue(_is_open(graph))


class TestCortexServiceClosesAllThree(_IsolatedStoreTest):
    """Task 6 verification #2: close() closes vector + graph + queue."""

    def _service(self):
        return CortexService(
            rules=self.rules,
            vector_store=VectorStore(":memory:"),
            graph_store=GraphStore(":memory:"),
            queue=EventQueue(self.path("q.db")),
        )

    def test_close_closes_vector_graph_and_queue(self):
        service = self._service()
        vector, graph, queue = service.vector, service.graph, service.queue

        for label, store in (("vector", vector), ("graph", graph), ("queue", queue)):
            self.assertTrue(_is_open(store), "%s should start open" % label)

        service.close()

        assert_closed(self, vector, "vector store")
        assert_closed(self, graph, "graph store")
        assert_closed(self, queue, "queue")

    def test_close_is_idempotent(self):
        service = self._service()
        service.close()
        service.close()
        self.assertTrue(service.closed)

    def test_context_manager_closes_all_three(self):
        with self._service() as service:
            handles = (service.vector, service.graph, service.queue)
        for store in handles:
            self.assertFalse(_is_open(store))

    def test_detached_store_is_still_closed(self):
        """Degradation detaches a store; close() must still release it.

        Several tests (and the degraded runtime path) mark a store unusable by
        setting ``service.vector = None``. A close() that only walked the live
        attributes would leave that connection open until GC - one of the
        original ResourceWarning sources.
        """
        service = self._service()
        vector = service.vector
        service.vector = None

        service.close()

        assert_closed(self, vector, "detached vector store")

    def test_all_three_detached_are_still_closed(self):
        service = self._service()
        handles = (service.vector, service.graph, service.queue)
        service.vector = None
        service.graph = None
        service.queue = None

        service.close()

        for store in handles:
            self.assertFalse(_is_open(store), "a detached handle was left open")


class TestPartialConstructionDoesNotLeak(_IsolatedStoreTest):
    """If the second store fails to open, the first must not be stranded.

    ContextBuilder/Curator open the vector store first. When the graph store
    then raises, the vector handle is already open but no caller ever receives
    a reference to it - so nothing could close it. The constructor closes it
    before propagating.
    """

    def _with_failing_graph(self, factory):
        import core.graph_store as gsmod

        opened = {}
        real_graph_init = gsmod.GraphStore.__init__
        real_vector_init = VectorStore.__init__

        def capture_vector(self, *args, **kwargs):
            real_vector_init(self, *args, **kwargs)
            opened["vector"] = self

        def fail_graph(self, *args, **kwargs):
            raise RuntimeError("graph store refused to open")

        VectorStore.__init__ = capture_vector
        gsmod.GraphStore.__init__ = fail_graph
        try:
            with self.assertRaises(RuntimeError):
                factory()
        finally:
            VectorStore.__init__ = real_vector_init
            gsmod.GraphStore.__init__ = real_graph_init

        self.assertIn("vector", opened, "the vector store was constructed")
        assert_closed(self, opened["vector"], "orphaned vector store")

    def test_context_builder_closes_vector_when_graph_fails(self):
        self._with_failing_graph(lambda: ContextBuilder(rules=self.rules, use_ripgrep=False))

    def test_curator_closes_vector_when_graph_fails(self):
        self._with_failing_graph(lambda: Curator(rules=self.rules))


class TestNoResourceWarningOnDrop(unittest.TestCase):
    """The GC-time backstop: dropping an unclosed store must stay silent."""

    def test_dropping_an_unclosed_store_emits_no_resource_warning(self):
        for factory in (
            lambda: VectorStore(":memory:"),
            lambda: GraphStore(":memory:"),
        ):
            with self.subTest(factory=factory):
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    store = factory()
                    del store
                    gc.collect()
                resource_warnings = [
                    w for w in caught if issubclass(w.category, ResourceWarning)
                ]
                self.assertEqual(
                    [str(w.message) for w in resource_warnings], [],
                    "dropping a store must not emit ResourceWarning",
                )


if __name__ == "__main__":
    unittest.main()
