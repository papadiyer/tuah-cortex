"""Golden-set evaluation for expert-axis routing (acceptance criterion 4).

This is an EVAL, not a pass/fail gate on quality. It reports route accuracy and
leakage for routed vs global retrieval and prints the numbers. Deliberately no
hardcoded quality target is asserted: the design doc (section 9) forbids
baselining weights on a made-up number, and a test that asserted
"accuracy >= 0.8" would either be vacuous or would start failing for whoever
next tunes the keyword lists.

What IS asserted is the property the feature must hold regardless of tuning:

  * routed retrieval never returns fewer memories than global retrieval
    (the no-regression guarantee - routing must never lose recall);
  * every prompt with an expected axis activates at least that axis.

Run the numbers on their own with::

    python3 -m unittest tests.test_expert_routing_eval -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.context_builder import ContextBuilder  # noqa: E402
from core.graph_store import GraphStore  # noqa: E402
from core.memory_curator import Curator  # noqa: E402
from core.rules import load_rules  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402
from tests.support import closing  # noqa: E402

GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "expert_routing.jsonl")

# Corpus spanning the same domains as the golden prompts. Each memory is written
# through the CURATOR (not straight into the store) so the eval measures the real
# tag-then-route loop rather than a hand-tagged fixture.
#
# It deliberately contains CROSS-DOMAIN DISTRACTORS - memories that share
# vocabulary with another domain ("service", "network", "scale", "cost",
# "model"). Without them the corpus is too easy: plain cosine already surfaces
# the right memory, routing has nothing to promote, and the eval flatters the
# feature. Retrieval quality is only meaningful when the store contains things
# that are plausibly-but-wrongly similar.
CORPUS = [
    "The customer SLA for the 5G private network guarantees 99.9 percent uptime with service credits",
    "Our BOQ for the RAN rollout lists baseband units, antennas and the commercial installation fee",
    "The RFP response must cover core network redundancy and the bandwidth commitment",
    "Presales rule: never quote a tender price before the BOQ is signed off",
    "Proxmox cluster uses ZFS mirrors; a scrub runs weekly via cron on Debian",
    "When systemd fails to start the daemon, journalctl -xe shows the unit error first",
    "The fstab entry needs nofail so a missing LVM volume does not block boot on Ubuntu",
    "iptables rules are restored at boot; ssh from the LAN must stay permitted",
    "The vector store ranks knowledge by cosine similarity over a 512 dimension embedding",
    "The context builder merges knowledge and experience under a hard token budget",
    "Docker containers run the inference api behind a latency budget of 200ms",
    "RAG retrieval uses the embedding model identity to refuse mixed vector spaces",
    "Architecture decision: we accepted tech debt in the queue to ship the roadmap on time",
    "The build vs buy trade-off favoured buying observability and building the memory layer",
    "Governance rule from the postmortem: every outage gets a written architecture decision record",
    "Founder decision: keep the burn rate flat until unit economics on the MVP are proven",
    "Pricing experiment showed the GTM motion works better with a per-seat model",
    "We chose to hire one engineer instead of extending runway by cutting the product scope",
    # -- cross-domain distractors: similar words, wrong domain ---------------
    "The office network printer keeps dropping its connection to the shared drive",
    "Service desk ticket volume doubled after the holiday, we need a rota",
    "The customer feedback form asks about response time and general satisfaction",
    "Scale the training workshop to two cohorts so more of the team can attend",
    "Cost of the annual team offsite came in under the budget we set",
    "The model railway club meets monthly and rents the community hall",
    "Uptime of the coffee machine is now a running joke in the standup",
    "Our commercial lease renews next year and the landlord wants a longer term",
    "Bandwidth for the video call was fine once everyone turned cameras off",
    "The kernel of the argument was that we shipped before the design was agreed",
    "Token gestures in the retro do not fix the underlying process problem",
    "Architecture of the new office puts the quiet room next to the kitchen",
]


def _load_golden():
    rows = []
    with open(GOLDEN_PATH, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if "prompt" in row:  # skip the leading _comment row
                rows.append(row)
    return rows


class TestExpertRoutingGoldenSet(unittest.TestCase):
    """Route accuracy + leakage, routed vs global. Numbers reported."""

    @classmethod
    def setUpClass(cls):
        cls.golden = _load_golden()
        cls.rules = load_rules()

    def setUp(self):
        self.vector = closing(self, VectorStore(":memory:"))
        self.graph = closing(self, GraphStore(":memory:"))
        curator = closing(self, Curator(vector_store=self.vector, graph_store=self.graph))
        # Ingest through the curator so the corpus carries real, derived tags.
        for text in CORPUS:
            kind = curator.classify(text)
            experts, confidence = curator.expert_tags(text, kind=kind)
            self.vector.add(
                text,
                {"source": "golden_corpus", "experts": experts, "expert_confidence": confidence},
            )
        self.builder = closing(
            self,
            ContextBuilder(
                vector_store=self.vector, graph_store=self.graph, use_ripgrep=False
            ),
        )

    def test_golden_set_is_well_formed(self):
        """The set must be 30-50 real prompts spanning the five categories."""
        self.assertGreaterEqual(len(self.golden), 30, "golden set too small to be meaningful")
        self.assertLessEqual(len(self.golden), 50)
        categories = {row["category"] for row in self.golden}
        self.assertEqual(
            categories,
            {
                "telco_proposal",
                "linux_troubleshooting",
                "m5_architecture",
                "founder_decision",
                "mixed_domain",
                "no_axis",
            },
        )
        self.assertEqual(
            len({row["id"] for row in self.golden}), len(self.golden), "duplicate golden ids"
        )

    def test_expected_axis_is_always_activated(self):
        """Every prompt with an expected axis must fire at least that axis.

        This is the one routing property worth asserting: whatever the weights,
        a telco prompt that does not activate the telco lens is a routing bug,
        not a tuning preference.
        """
        misses = []
        for row in self.golden:
            expected = row.get("expect")
            if not expected:
                continue
            routing = self.builder.route_experts(row["prompt"])
            if expected not in routing:
                misses.append((row["id"], expected, sorted(routing)))
        self.assertEqual(misses, [], "prompts whose expected axis never fired: %r" % (misses,))

    def test_no_axis_prompts_fall_back_to_global(self):
        """A domain-free prompt must route nothing and still retrieve."""
        for row in self.golden:
            if row.get("expect") is not None:
                continue
            routing = self.builder.route_experts(row["prompt"])
            self.assertEqual(
                routing, {}, "%s should not activate any axis, got %r" % (row["id"], routing)
            )

    def test_routing_never_loses_recall_versus_global(self):
        """No-regression guarantee: routed retrieval >= global retrieval size.

        The global recall floor exists precisely so enabling routing cannot
        shrink the candidate pool. If this fails, routing has become a filter
        instead of a lens.
        """
        shrunk = []
        for row in self.golden:
            prompt = row["prompt"]
            routed = self.builder.retrieve(prompt)
            n_routed = len(routed["knowledge"]) + len(routed["experience"])
            n_global = len(
                self.vector.query(prompt, int(self.rules["retrieval"]["vector_top_k"]))
            ) + len(self.graph.query(prompt, int(self.rules["retrieval"]["graph_top_k"])))
            if n_routed < n_global:
                shrunk.append((row["id"], n_routed, n_global))
        self.assertEqual(shrunk, [], "routing lost recall on: %r" % (shrunk,))

    def test_report_route_accuracy_and_leakage(self):
        """Report routed vs global quality. Numbers printed, not asserted."""
        top1_hits = 0
        scored = 0
        leaked_axes = 0
        total_axes = 0

        routed_relevant = 0
        routed_total = 0
        global_relevant = 0
        global_total = 0

        for row in self.golden:
            prompt = row["prompt"]
            expected = row.get("expect")
            allowed = set(row.get("accept") or [])
            routing = self.builder.route_experts(prompt)

            if expected:
                scored += 1
                ranked = sorted(routing, key=lambda a: (-routing[a], a))
                if ranked and ranked[0] == expected:
                    top1_hits += 1
                # Leakage: an axis fired that is neither expected nor accepted.
                for axis in routing:
                    total_axes += 1
                    if axis != expected and axis not in allowed:
                        leaked_axes += 1

                # Retrieval-level relevance: a retrieved memory counts as
                # relevant when it carries the expected axis tag.
                routed = self.builder.retrieve(prompt)
                for entry in routed["knowledge"]:
                    routed_total += 1
                    if expected in (entry.get("experts") or []):
                        routed_relevant += 1
                for entry in self.vector.query(
                    prompt, int(self.rules["retrieval"]["vector_top_k"])
                ):
                    global_total += 1
                    if expected in (entry.get("experts") or []):
                        global_relevant += 1

        def pct(num, den):
            return (100.0 * num / den) if den else 0.0

        report = {
            "golden_prompts": len(self.golden),
            "scored_prompts": scored,
            "route_top1_accuracy_pct": round(pct(top1_hits, scored), 1),
            "axis_leakage_pct": round(pct(leaked_axes, total_axes), 1),
            "routed_precision_pct": round(pct(routed_relevant, routed_total), 1),
            "global_precision_pct": round(pct(global_relevant, global_total), 1),
            "routed_irrelevant_pct": round(100.0 - pct(routed_relevant, routed_total), 1),
            "global_irrelevant_pct": round(100.0 - pct(global_relevant, global_total), 1),
        }
        print("\n[expert-routing golden eval] %s" % json.dumps(report, indent=2))

        # Only structural sanity is asserted - the quality numbers are evidence
        # for a human, not a threshold for CI.
        self.assertEqual(scored + 3, len(self.golden), "unscored prompts beyond the 3 neutral ones")
        self.assertGreater(routed_total, 0, "eval retrieved nothing; corpus or routing is broken")


if __name__ == "__main__":
    unittest.main()
