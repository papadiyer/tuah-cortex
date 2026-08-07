# Expert-Axis Routing v0.5 — Cortex "Mixture-of-Experts" (knowledge + persona)

**Status:** Design for review. NOT an RC1 blocker. GA / v0.5 hardening candidate.
**Scope:** additive schema + retrieval routing. No rewrite of RC1 store code.
**Bundles with:** semantic-store rebuild (multilingual ST embedder flip) — re-ingest
`--from-hermes` once, not twice.

## 0. What this actually is (and is not)

- This is a **Mixture-of-Experts at the memory/persona layer**, NOT an LLM MoE.
  Jebat keeps one stable identity; expertise lenses are *selectively activated*
  per query. The LLM itself (Nous remote or future local MoE) is untouched.
- Two distinct primitives, never conflated:
  - **Persona (Tier 0, always-on):** abah-abah voice + CTO/founder default stance.
    Sourced from stable config, never from cosine recall. If persona were
    retrieval-based, Jebat would be inconsistent.
  - **Expertise axes (Tier 1, retrieval lens):** telco_presales, linux_pro,
    technologist, cto, founder. Activated only when the query is in-domain.

## 1. Expert axis vocabulary

| Axis | Kind | Activates on | Tagging scope |
|---|---|---|---|
| `abah_abah` (voice) | Tier 0 persona | always | identity.json only, never a memory tag |
| `cto` (stance) | Tier 0 default + corpus tag | always (stance) / experience only (tag) | identity.json + Experience memories (architecture trade-off, build-vs-buy, governance, product decisions, failure lessons) |
| `founder` (stance) | Tier 0 default + corpus tag | always (stance) / experience only (tag) | identity.json + Experience memories (same gating as cto) |
| `telco_presales` | Tier 1 lens | query in telco/proposal domain | Knowledge + Experience |
| `linux_pro` | Tier 1 lens | query in sysadmin/infra domain | Knowledge + Experience |
| `technologist` | Tier 1 lens | query in general-tech domain | Knowledge + Experience |

**Rule (from review):** never label a memory `cto`/`founder` just because Jebat
*sounds* founder-minded. Those corpus tags apply ONLY to Experience memories
that are genuinely architecture/governance/product/failure lessons.

## 2. Schema (additive — mirrors existing `_ADDITIVE_COLUMNS` pattern)

### `config/cortex_rules.json` — new `expert_axes` section
Follows the existing `classification` keyword-list shape (no new infra):

```json
"expert_axes": {
  "default_weight": 0.0,
  "axes": {
    "telco_presales": {
      "keywords": ["proposal","sla","5g","private network","boq","rfi","presales","customer","commercial","ran","core network"],
      "weight": 1.0
    },
    "linux_pro": {
      "keywords": ["proxmox","zfs","ssh","systemd","debian","ubuntu","fstab","cron","iptables","firewall","lvm","kernel","daemon"],
      "weight": 1.0
    },
    "technologist": {
      "keywords": ["api","architecture","model","inference","vector","embedding","rag","docker","kubernetes","gpu","token"," latency"],
      "weight": 0.8
    },
    "cto": {
      "keywords": ["trade-off","build vs buy","governance","architecture decision","tech debt","roadmap","buy vs build"],
      "weight": 1.0, "experience_only": true
    },
    "founder": {
      "keywords": ["gtm","burn rate","pricing","mvp","product-market","hire","funding","pivot","unit economics"],
      "weight": 1.0, "experience_only": true
    }
  }
}
```

### `core/vector_store.py` — extend `_ADDITIVE_COLUMNS`
Add `("experts", "TEXT")` and `("expert_confidence", "TEXT")`. JSON-encoded
list + dict. Additive migration in `_ensure_columns()` — existing rows keep
NULL and behave as "no axis" (backward compatible). `search()`/`query()` already
support arbitrary SQL filters; add `experts` filter (LIKE per axis).

### `core/graph_store.py` — same additive `experts`/`expert_confidence` on edges
Experience memories get the same tagging; cto/founder tags land here most.

## 3. Curator — deterministic tagging + confidence (no hard truth)

`core/memory_curator.py`:
- New `score_experts(text)` reusing `keyword_overlap` against `expert_axes`.
- `process_message()` record gains `experts: List[str]` (axes above threshold)
  and `expert_confidence: Dict[str, float]` (normalised 0–1 per axis).
- `ingest()` / `ingest_event()` pass `experts` + `expert_confidence` into the
  store meta + new columns.
- **Confidence, not boolean:** a segment mentioning SLA yields
  `experts=["telco_presales","technologist"]`,
  `expert_confidence={"telco_presales":0.85,"technologist":0.55}`.
- cto/founder tags are gated: only attached when `kind == "experience"` AND the
  axis has `experience_only: true`. Prevents over-labeling.
- This is a *heuristic* ingestion contract. Schema is ready for later LLM-assisted
  tagging — we do NOT lock to "tag exists / absent" only (per review).
- `ingest()` already calls `check_compatibility(raise_on_mismatch=True)` — expert
  columns do not affect embedding identity, so RC1 stays intact.

## 4. Context Builder — routing influences CANDIDATE GENERATION

`core/context_builder.py::retrieve()` reorder (per review — not rerank-after):

```
Query
  -> Intent/Domain routing:  route_experts(prompt) -> axis weights w[]
  -> Project scope:          existing project filter
  -> Expert lens weighting:  build per-axis candidate queries
  -> Semantic retrieval:     per-axis vector.query(prompt, top_k_axis, project, experts=<axis>)
  -> Experience/rule boost:  cto/founder experience edges boosted when w[cto]/w[founder] high
  -> Budget blend:           merge under token_budget, keep Tier 0 persona fixed
```

- `route_experts(prompt)` reuses `keyword_overlap(prompt, axis.keywords)`; if no
  axis fires, fall back to balanced global retrieval (current behaviour) so we
  never degrade below RC1.
- Per-axis retrieval means the expert axis shapes WHICH memories enter the
  candidate pool, not just reorders a global pool that may already contain
  irrelevant memories.
- Response gains `expert_routing: {"telco_presales":0.9, "technologist":0.6, ...}`
  so the caller (Tuah/Jebat) sees routing — additive field in API contract.

## 5. Tier 0 identity (`config/identity.json`)

Add (additive JSON keys):
```json
"persona": {
  "voice": "abah_abah",
  "voice_spec": "casual EN+MS mixing, warm, direct, no corporate fluff",
  "default_stance": ["cto", "founder"],
  "expert_axes": ["telco_presales","linux_pro","technologist","cto","founder"]
}
```
This tells the router the valid axis vocabulary and keeps persona always-on,
independent of retrieval. No secret, no schema break.

## 6. API contract (`docs/API_CONTRACTS.md`)

`POST /v1/context/build` response: add `expert_routing` (object of axis→weight).
Everything else unchanged. `/v1/memory/search` gains `experts` filter (reuses
existing filter path). No new endpoint → no new security surface.

## 7. Evaluation — mandatory before weighting is baselined

Build a **golden query set** (30–50 real prompts from Faisal's own work):
- telco proposal, Linux troubleshooting, M5 architecture, founder decision,
  mixed-domain task.
For each: expert route → retrieved memories → useful / not → irrelevant leakage
→ answer quality. Compare **expert-routed retrieval vs current global retrieval**.
Only baseline weights empirically. Without this, MoE is just clever heuristic
routing (per review). Stored as `tests/golden/expert_routing.jsonl`; a small
eval script reports route accuracy + leakage rate.

## 8. Acceptance criteria (5)

1. **Additive schema** — new `experts`/`expert_confidence` columns + `expert_axes`
   rules + identity keys; RC1 unit tests (314) stay green; existing stores open
   without re-migration.
2. **Deterministic expert tagging + confidence** — curator emits `experts` list
   + `expert_confidence` dict; cto/founder gated to Experience; reproducible.
3. **Per-lens retrieval/blending under token budget** — `route_experts` shapes
   candidate generation; final digest respects `user_char_limit`/`memory_char_limit`
   and `token_budget`; falls back to global retrieval when no axis fires.
4. **Golden-set eval** — expert routing beats current global retrieval on the
   30–50 prompt set (route accuracy up, leakage down); numbers reported, not asserted.
5. **Config-only tunability** — a NEW expertise axis (e.g. `cloud_arch`) is added
   purely by editing `cortex_rules.json` (`expert_axes`) + `identity.json`
   (`persona.expert_axes`); the runtime routes it and tags memories for it with
   NO code change; RC1 unit tests still green; golden-set re-run confirms the
   new axis is picked up. Proves the stack is tunable by data, not by rewrite.

## 9. Non-goals / caveats

- Not an RC1 blocker. Ships as v0.5 GA-hardening.
- Re-ingest required (dim-512 → semantic + expert tags) — one pass, see runbook.
- Hardcoded weighting is explicitly forbidden; weights come from §7 eval.
- No GPU needed — pure metadata + retrieval weighting.
