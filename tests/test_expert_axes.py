"""Expert-axis routing v0.5: tagging, schema additivity and routed retrieval.

Covers the acceptance criteria that are properties rather than measurements
(the measured ones live in tests/test_expert_routing_eval.py):

  1. additive schema - new columns, old rows still readable, embedding identity
     guard untouched by expert tagging;
  2. deterministic tagging with the cto/founder Experience gate;
  3. routed candidate generation with a global fallback;
  5. config-only tunability - a brand-new axis works with no code change.
"""

import copy
import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.context_builder import ContextBuilder  # noqa: E402
from core.graph_store import GraphStore  # noqa: E402
from core.memory_curator import Curator  # noqa: E402
from core.rules import Embedder, active_axes, load_rules, score_axes  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402
from tests.support import closing  # noqa: E402

TELCO = "The customer SLA in the 5G private network RFP needs a BOQ and commercial terms"
LINUX = "Proxmox refuses to mount the ZFS pool; check systemd, fstab and journalctl on Debian"
CTO_TEXT = "Architecture decision: the build vs buy trade-off on governance and tech debt"
FOUNDER_TEXT = "Founder call on pricing, burn rate, runway and product-market fit for the MVP"


def _curator(testcase, rules=None):
    return closing(
        testcase,
        Curator(
            rules=rules,
            vector_store=closing(testcase, VectorStore(":memory:", rules=rules)),
            graph_store=closing(testcase, GraphStore(":memory:", rules=rules)),
        ),
    )


class TestScoreExperts(unittest.TestCase):
    """Criterion 2: deterministic tagging + confidence, cto/founder gated."""

    def setUp(self):
        self.curator = _curator(self)

    def test_telco_segment_scores_telco_high(self):
        scores = self.curator.score_experts(TELCO, kind="knowledge")
        self.assertIn("telco_presales", scores)
        self.assertGreater(scores["telco_presales"], 0.5)
        self.assertEqual(
            max(scores, key=lambda a: scores[a]),
            "telco_presales",
            "telco segment should be dominated by the telco axis, got %r" % (scores,),
        )

    def test_confidence_is_bounded_zero_to_one(self):
        for text in (TELCO, LINUX, CTO_TEXT, FOUNDER_TEXT):
            for axis, score in self.curator.score_experts(text, kind="experience").items():
                self.assertGreaterEqual(score, 0.0, axis)
                self.assertLessEqual(score, 1.0, axis)

    def test_tagging_is_reproducible(self):
        first = self.curator.expert_tags(TELCO, kind="knowledge")
        for _ in range(5):
            self.assertEqual(self.curator.expert_tags(TELCO, kind="knowledge"), first)

    def test_cto_and_founder_never_attach_to_knowledge(self):
        """The gate from EXPERT_AXIS_ROUTING_v0.5.md section 3."""
        for text in (CTO_TEXT, FOUNDER_TEXT):
            axes, scores = self.curator.expert_tags(text, kind="knowledge")
            self.assertNotIn("cto", axes, text)
            self.assertNotIn("founder", axes, text)
            self.assertNotIn("cto", scores, text)
            self.assertNotIn("founder", scores, text)

    def test_cto_and_founder_do_attach_to_experience(self):
        self.assertIn("cto", self.curator.expert_tags(CTO_TEXT, kind="experience")[0])
        self.assertIn("founder", self.curator.expert_tags(FOUNDER_TEXT, kind="experience")[0])

    def test_non_gated_axis_attaches_to_knowledge(self):
        """Only experience_only axes are gated; telco/linux are not."""
        self.assertIn("telco_presales", self.curator.expert_tags(TELCO, kind="knowledge")[0])
        self.assertIn("linux_pro", self.curator.expert_tags(LINUX, kind="knowledge")[0])

    def test_unrelated_text_tags_nothing(self):
        axes, scores = self.curator.expert_tags("Remind me to call my mother", kind="knowledge")
        self.assertEqual(axes, [])
        self.assertEqual(scores, {})

    def test_active_axes_orders_by_confidence_then_name(self):
        ordered = active_axes({"b": 0.4, "a": 0.9, "c": 0.4}, {"min_confidence": 0.0})
        self.assertEqual(ordered, ["a", "b", "c"])


class TestProcessMessageTagging(unittest.TestCase):
    """process_message() records carry experts + expert_confidence."""

    def setUp(self):
        self.curator = _curator(self)

    def _records(self, content):
        import asyncio

        return asyncio.run(self.curator.process_message({"role": "user", "content": content}, "s.jsonl"))

    def test_record_gains_expert_fields(self):
        records = self._records(TELCO)
        self.assertTrue(records)
        for record in records:
            self.assertIn("experts", record)
            self.assertIn("expert_confidence", record)
            self.assertIsInstance(record["experts"], list)
            self.assertIsInstance(record["expert_confidence"], dict)

    def test_knowledge_record_is_never_tagged_cto(self):
        for record in self._records(CTO_TEXT):
            if record["kind"] == "knowledge":
                self.assertNotIn("cto", record["experts"])


class TestVectorStoreExperts(unittest.TestCase):
    """Criterion 1: additive columns, round-trip, NULL-axis back-compat."""

    def setUp(self):
        self.store = closing(self, VectorStore(":memory:"))

    def test_round_trip(self):
        self.store.add(
            TELCO,
            {
                "source": "t",
                "experts": ["telco_presales", "technologist"],
                "expert_confidence": {"telco_presales": 0.86, "technologist": 0.44},
            },
        )
        entry = self.store.all_entries()[0]
        self.assertEqual(entry["experts"], ["telco_presales", "technologist"])
        self.assertEqual(entry["expert_confidence"]["telco_presales"], 0.86)

    def test_untagged_row_reads_as_empty_not_null(self):
        self.store.add("a plain memory with no domain at all", {"source": "t"})
        entry = self.store.all_entries()[0]
        self.assertEqual(entry["experts"], [])
        self.assertEqual(entry["expert_confidence"], {})

    def test_query_filters_by_axis(self):
        self.store.add(TELCO, {"source": "t", "experts": ["telco_presales"]})
        self.store.add(LINUX, {"source": "t", "experts": ["linux_pro"]})
        hits = self.store.query("sla 5g private network boq", experts=["telco_presales"])
        self.assertTrue(hits)
        for hit in hits:
            self.assertIn("telco_presales", hit["experts"])
        self.assertEqual(self.store.query(TELCO, experts=["founder"]), [])

    def test_search_filters_by_axis(self):
        self.store.add(TELCO, {"source": "t", "experts": ["telco_presales"]})
        self.store.add(LINUX, {"source": "t", "experts": ["linux_pro"]})
        found = self.store.search(filters={"experts": ["linux_pro"]})
        self.assertEqual(len(found), 1)
        self.assertIn("linux_pro", found[0]["experts"])

    def test_axis_filter_does_not_match_a_prefix(self):
        """'cto' must not match an axis named 'cto_advisory'."""
        self.store.add("a governance memory", {"source": "t", "experts": ["cto_advisory"]})
        self.assertEqual(self.store.search(filters={"experts": ["cto"]}), [])
        self.assertEqual(len(self.store.search(filters={"experts": ["cto_advisory"]})), 1)

    def test_untagged_rows_are_excluded_by_an_axis_filter(self):
        """A lens is strict: NULL-axis rows do not match (documented behaviour)."""
        self.store.add("legacy untagged memory about networks", {"source": "t"})
        self.assertEqual(self.store.search(filters={"experts": ["telco_presales"]}), [])
        self.assertEqual(len(self.store.search(filters={})), 1)

    def test_legacy_database_without_expert_columns_is_migrated(self):
        """Criterion 1: an existing store opens without manual re-migration."""
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "_legacy_expert_test.db"
        )
        if os.path.exists(path):
            os.remove(path)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        # A pre-v0.5 database: the base schema, no expert columns at all.
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE knowledge (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " text TEXT NOT NULL, embedding TEXT NOT NULL, source TEXT, ts TEXT,"
            " type TEXT NOT NULL DEFAULT 'knowledge', meta TEXT,"
            " fingerprint TEXT UNIQUE, embed_meta TEXT);"
        )
        conn.execute(
            "INSERT INTO knowledge (text, embedding, source, fingerprint)"
            " VALUES ('legacy row', ?, 'old', 'old::legacy row')",
            (json.dumps([0.0] * 512),),
        )
        conn.commit()
        conn.close()

        store = closing(self, VectorStore(path))
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(knowledge)")}
        self.assertIn("experts", cols)
        self.assertIn("expert_confidence", cols)
        entry = store.all_entries()[0]
        self.assertEqual(entry["text"], "legacy row")
        self.assertEqual(entry["experts"], [])


class TestEmbeddingIdentityStillGuarded(unittest.TestCase):
    """Criterion 1: expert columns are NOT part of embedding identity."""

    class _OtherEmbedder(Embedder):
        name = "other-backend"

        @property
        def dimensions(self):
            return 512

        @property
        def model(self):
            return "other-model"

        def embed(self, text):
            return [0.5] * 512

    def test_mixed_vectors_are_still_refused_when_tagged(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_identity_expert.db")
        if os.path.exists(path):
            os.remove(path)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        store = VectorStore(path)
        store.add(TELCO, {"source": "t", "experts": ["telco_presales"]})
        store.close()

        # Same dimension, different backend/model: must still fail loud even
        # though both rows carry expert tags.
        other = closing(self, VectorStore(path, embedder=self._OtherEmbedder()))
        with self.assertRaises(ValueError):
            other.check_compatibility(raise_on_mismatch=True)
        with self.assertRaises(ValueError):
            other.add("a new telco sla memory", {"experts": ["telco_presales"]})

    def test_expert_tags_do_not_appear_in_identity(self):
        store = closing(self, VectorStore(":memory:"))
        store.add(TELCO, {"source": "t", "experts": ["telco_presales"]})
        report = store.check_compatibility()
        self.assertEqual(report["mismatched"], 0)
        self.assertNotIn("experts", report)


class TestGraphStoreExperts(unittest.TestCase):
    def setUp(self):
        self.graph = closing(self, GraphStore(":memory:"))

    def test_edge_round_trip_and_filter(self):
        self.graph.add_edge(
            "proxmox.conf",
            "uses",
            "zfs",
            meta={"experts": ["linux_pro"], "expert_confidence": {"linux_pro": 0.8}},
        )
        self.graph.add_edge("proposal.md", "contains", "sla", meta={"experts": ["telco_presales"]})

        hits = self.graph.query("proxmox zfs", experts=["linux_pro"])
        self.assertTrue(hits)
        self.assertEqual(hits[0]["experts"], ["linux_pro"])
        self.assertEqual(hits[0]["expert_confidence"], {"linux_pro": 0.8})
        self.assertEqual(self.graph.query("proxmox zfs", experts=["telco_presales"]), [])

    def test_untagged_edge_reads_as_empty(self):
        self.graph.add_edge("a.py", "imports", "b.py")
        hit = self.graph.query("a.py imports b.py")[0]
        self.assertEqual(hit["experts"], [])
        self.assertEqual(hit["expert_confidence"], {})


class TestRouteExperts(unittest.TestCase):
    """Criterion 3: routing shapes candidate generation, with global fallback."""

    def setUp(self):
        self.builder = closing(
            self,
            ContextBuilder(
                vector_store=closing(self, VectorStore(":memory:")),
                graph_store=closing(self, GraphStore(":memory:")),
                use_ripgrep=False,
            ),
        )

    def test_linux_prompt_routes_linux_pro_dominant(self):
        routing = self.builder.route_experts(
            "proxmox zfs pool will not mount, systemd and fstab on debian"
        )
        self.assertIn("linux_pro", routing)
        self.assertEqual(max(routing, key=lambda a: routing[a]), "linux_pro", routing)

    def test_telco_prompt_routes_telco_dominant(self):
        routing = self.builder.route_experts("draft the SLA and BOQ for the 5G private network RFP")
        self.assertEqual(max(routing, key=lambda a: routing[a]), "telco_presales", routing)

    def test_routing_is_not_gated_by_kind(self):
        """A cto *question* activates the cto lens even though the cto *tag*
        is restricted to Experience memories at write time."""
        routing = self.builder.route_experts("what is the build vs buy trade-off here?")
        self.assertIn("cto", routing)

    def test_no_axis_prompt_returns_empty_routing(self):
        self.assertEqual(self.builder.route_experts("remind me what we said yesterday"), {})

    def test_retrieve_falls_back_to_global_when_no_axis_fires(self):
        """Criterion 3: never worse than RC1."""
        self.builder.vector.add("yesterday we agreed to meet on tuesday", {"source": "t"})
        retrieved = self.builder.retrieve("what did we agree yesterday")
        self.assertEqual(retrieved["expert_routing"], {})
        self.assertTrue(retrieved["knowledge"], "global fallback returned nothing")

    def test_retrieve_reports_routing_and_still_returns_untagged_memory(self):
        """The global recall floor: untagged rows survive a routed query."""
        self.builder.vector.add(
            "legacy note about the sla we promised the customer", {"source": "t"}
        )
        retrieved = self.builder.retrieve("what SLA did we promise in the 5G RFP?")
        self.assertIn("telco_presales", retrieved["expert_routing"])
        self.assertTrue(
            retrieved["knowledge"],
            "an untagged but relevant memory was lost by routing - recall regression",
        )

    def test_routed_entries_carry_routed_by(self):
        self.builder.vector.add(TELCO, {"source": "t", "experts": ["telco_presales"]})
        retrieved = self.builder.retrieve("SLA and BOQ for the 5G private network RFP")
        tagged = [e for e in retrieved["knowledge"] if e.get("routed_by")]
        self.assertTrue(tagged, "no entry recorded which axis retrieved it")
        self.assertIn("telco_presales", tagged[0]["routed_by"])

    def test_build_exposes_expert_routing(self):
        context = self.builder.build("proxmox zfs systemd debugging")
        self.assertIn("expert_routing", context)
        self.assertIn("linux_pro", context["expert_routing"])

    def test_build_context_exposes_routing_and_persona(self):
        payload = self.builder.build_context({"prompt": "SLA for the 5G private network RFP"})
        self.assertIn("telco_presales", payload["expert_routing"])
        # Persona is Tier 0 and always-on, independent of what was retrieved.
        self.assertEqual(payload["persona"].get("voice"), "abah_abah")
        self.assertIn("cto", payload["persona"].get("default_stance", []))

    def test_budget_still_enforced_under_routing(self):
        """Criterion 3: routing does not let the digest exceed its budget."""
        for i in range(40):
            self.builder.vector.add(
                "sla proposal detail %02d for the 5g private network %s"
                % (i, "boq commercial ran core network " * 20),
                {"source": "flood", "experts": ["telco_presales"]},
            )
        payload = self.builder.build_context(
            {"prompt": "SLA and BOQ for the 5G private network", "token_budget": 300}
        )
        self.assertLessEqual(len(payload["context_markdown"]), 300 * 4)
        context = self.builder.build("SLA and BOQ for the 5G private network")
        self.assertLessEqual(context["memory_block_chars"], self.builder.memory_char_limit)


class TestConfigOnlyTunability(unittest.TestCase):
    """Criterion 5: a NEW axis works with NO code change."""

    def setUp(self):
        # A brand-new axis that exists nowhere in the source tree.
        self.rules = copy.deepcopy(load_rules())
        self.rules["expert_axes"]["axes"]["cloud_arch"] = {
            "keywords": [
                "terraform",
                "vpc",
                "s3",
                "iam",
                "autoscaling",
                "cloudformation",
                "eks",
                "lambda",
            ],
            "weight": 1.0,
        }

    def test_new_axis_is_routed_without_code_change(self):
        builder = closing(
            self,
            ContextBuilder(
                rules=self.rules,
                vector_store=closing(self, VectorStore(":memory:", rules=self.rules)),
                graph_store=closing(self, GraphStore(":memory:", rules=self.rules)),
                use_ripgrep=False,
            ),
        )
        routing = builder.route_experts("terraform the vpc with iam roles and autoscaling on eks")
        self.assertIn("cloud_arch", routing)
        self.assertEqual(max(routing, key=lambda a: routing[a]), "cloud_arch", routing)

    def test_new_axis_tags_memory_without_code_change(self):
        curator = _curator(self, rules=self.rules)
        axes, scores = curator.expert_tags(
            "we provisioned the vpc and iam policy with terraform and cloudformation",
            kind="knowledge",
        )
        self.assertIn("cloud_arch", axes)
        self.assertGreater(scores["cloud_arch"], 0.5)

    def test_new_axis_round_trips_through_retrieval(self):
        vector = closing(self, VectorStore(":memory:", rules=self.rules))
        graph = closing(self, GraphStore(":memory:", rules=self.rules))
        curator = closing(
            self, Curator(rules=self.rules, vector_store=vector, graph_store=graph)
        )
        text = "the terraform vpc module provisions iam roles for autoscaling"
        axes, confidence = curator.expert_tags(text, kind="knowledge")
        vector.add(text, {"source": "t", "experts": axes, "expert_confidence": confidence})

        builder = closing(
            self,
            ContextBuilder(
                rules=self.rules, vector_store=vector, graph_store=graph, use_ripgrep=False
            ),
        )
        retrieved = builder.retrieve("terraform vpc iam autoscaling setup")
        self.assertIn("cloud_arch", retrieved["expert_routing"])
        self.assertTrue(retrieved["knowledge"])
        self.assertIn("cloud_arch", retrieved["knowledge"][0]["experts"])

    def test_axis_absent_from_config_is_never_routed(self):
        """The vocabulary is the config's, not the code's.

        Asserted against a config this test controls, NOT against whatever
        happens to be on disk: the tunability probe legitimately adds
        cloud_arch to the real config, and a test that broke because an
        operator tuned a config file would be testing the wrong thing.
        """
        stripped = copy.deepcopy(self.rules)
        stripped["expert_axes"]["axes"].pop("cloud_arch", None)
        builder = closing(
            self,
            ContextBuilder(
                rules=stripped,
                vector_store=closing(self, VectorStore(":memory:", rules=stripped)),
                graph_store=closing(self, GraphStore(":memory:", rules=stripped)),
                use_ripgrep=False,
            ),
        )
        routing = builder.route_experts("terraform the vpc with iam roles and autoscaling")
        self.assertNotIn("cloud_arch", routing)
        # An axis removed from config leaves no residue anywhere in the code.
        self.assertNotIn(
            "cloud_arch", json.dumps(builder.retrieve("terraform vpc iam")["expert_routing"])
        )


class TestScoreAxesUnit(unittest.TestCase):
    """The shared scorer used by both curator and builder."""

    def test_missing_config_scores_nothing(self):
        self.assertEqual(score_axes("anything at all", None), {})
        self.assertEqual(score_axes("anything at all", {}), {})

    def test_confidence_saturates_rather_than_growing_without_bound(self):
        cfg = {"saturation": 1.0, "axes": {"x": {"keywords": ["alpha", "beta", "gamma"], "weight": 1.0}}}
        one = score_axes("alpha", cfg)["x"]
        three = score_axes("alpha beta gamma", cfg)["x"]
        self.assertLess(one, three)
        self.assertLessEqual(three, 1.0)

    def test_weight_scales_confidence(self):
        light = {"axes": {"x": {"keywords": ["alpha"], "weight": 0.5}}}
        heavy = {"axes": {"x": {"keywords": ["alpha"], "weight": 1.0}}}
        self.assertLess(score_axes("alpha", light)["x"], score_axes("alpha", heavy)["x"])


if __name__ == "__main__":
    unittest.main()
