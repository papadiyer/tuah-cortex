# TASK 7 — Expert-Axis Routing v0.5 (GA hardening)

You are **Lekiu** (Claude Code), Builder under Jebat/Hermes supervision.
This task adds **Mixture-of-Experts routing at the memory + persona layer** to
Jebat-Cortex. It is NOT an LLM MoE — it is expertise/persona activation for
retrieval. Source of truth: `docs/EXPERT_AXIS_ROUTING_v0.5.md` (design), and the
files listed below. Read the design doc FIRST, then the real code.

## Context
RC1 froze `v0.4.0`. Cortex has a working semantic-ish vector store, graph store,
context builder, and event ingestion. We now add expert axes so Jebat activates
the right expertise (telco_presales / linux_pro / technologist / cto / founder)
per query, while keeping persona (abah_abah voice + cto/founder stance) always
on at Tier 0. Full rationale, schema, and gating rules are in the design doc.

## Hard constraints (from SECURITY_MODEL + repo CLAUDE.md)
- **Additive only.** Extend `_ADDITIVE_COLUMNS` / add config keys. Do NOT rewrite
  `core/vector_store.py` / `core/graph_store.py` retrieval logic destructively.
- `~/.hermes/state.db` read-only (never opened writable).
- Localhost only; no shell exec through API/MCP; no commit/push/merge/deploy.
- Keep **314 existing RC1 tests green**. New tests additive.
- Python 3.9+ syntax. Reuse `keyword_overlap`, `cosine`, `load_rules`,
  `_ADDITIVE_COLUMNS` patterns already present. No new dependency.
- `VectorStore` embedding-identity guard (`check_compatibility`) MUST stay intact.
  Expert columns are NOT part of embedding identity.
- Fail closed on missing config; never invent axes.

## Files to touch (read before edit)
- `config/cortex_rules.json` — add `expert_axes` section (see design §2).
- `config/identity.json` — additive `persona` block (design §5).
- `core/vector_store.py` — extend `_ADDITIVE_COLUMNS` with `experts` TEXT +
  `expert_confidence` TEXT; add `experts` SQL filter in `search()`/`query()`.
- `core/graph_store.py` — same additive columns on edges (design §2).
- `core/memory_curator.py` — `score_experts()`; emit `experts` + `expert_confidence`
  in `process_message()`; pass through `ingest()` + `ingest_event()`; gate
  cto/founder to `kind == "experience"`.
- `core/context_builder.py` — `route_experts(prompt)`; reorder `retrieve()` to
  per-axis candidate generation then budget blend (design §4); fallback to global
  when no axis fires; expose `expert_routing` in digest.
- `api/context.py` — include `expert_routing` in `/v1/context/build` response.
- `docs/API_CONTRACTS.md` — document `expert_routing` + `experts` filter (§6).
- `tests/` — new unit + fixture tests (see Verification).
- `tests/golden/expert_routing.jsonl` — golden query set (see §7 / Verification).

## 1. Config: expert_axes (cortex_rules.json)
Add an `expert_axes` section shaped like the existing `classification` keyword
lists. Axes: `telco_presales`, `linux_pro`, `technologist`, `cto`, `founder`.
`cto` and `founder` carry `"experience_only": true`. Reuse the keyword sets from
the design doc §2. No code change to add a future axis — it is data.

## 2. Schema: additive columns
`core/vector_store.py` `_ADDITIVE_COLUMNS` gains:
`("experts", "TEXT")`, `("expert_confidence", "TEXT")`.
`_ensure_columns()` already adds missing columns additively — existing rows keep
NULL and behave as "no axis" (backward compatible, RC1 tests stay green).
Same in `core/graph_store.py` for edges.

## 3. Curator: deterministic tagging + confidence
- `score_experts(text)` returns `{axis: confidence}` per `expert_axes` using
  `keyword_overlap` normalised 0–1 (relative to max hits for that axis).
- `process_message()` record gains `experts` (axes above a small threshold) and
  `expert_confidence` dict.
- `ingest()` / `ingest_event()`: pass `experts` + `expert_confidence` into meta AND
  the new columns. `check_compatibility(raise_on_mismatch=True)` is unaffected.
- **Gating:** cto/founder tags attach ONLY when `kind == "experience"` AND the
  axis has `experience_only: true`. This prevents over-labeling every reply as
  founder just because Jebat sounds founder-minded.
- This is a heuristic ingestion contract; schema must stay ready for later
  LLM-assisted tagging (do NOT lock to boolean "tag exists/absent").

## 4. Context Builder: routing shapes CANDIDATE GENERATION
Reorder `retrieve()` (design §4):
```
route_experts(prompt) -> axis weights w[]
-> project scope (existing)
-> per-axis vector.query(prompt, top_k_axis, project, experts=<axis>)
-> experience/rule boost: cto/founder edges boosted when w[cto]/w[founder] high
-> budget blend under token_budget; Tier 0 persona fixed
```
- If `route_experts` returns no axis above threshold, FALL BACK to the current
  global retrieval behaviour (never worse than RC1).
- Digest gains `expert_routing: {axis: weight}`.

## 5. Persona (Tier 0, additive) — identity.json
Add `persona`: `voice: "abah_abah"`, `voice_spec`, `default_stance: ["cto","founder"]`,
`expert_axes: [list]`. No secret, no schema break.

## 6. API contract (additive)
`POST /v1/context/build` response: add `expert_routing` (object). `/v1/memory/search`
gains `experts` filter reusing existing filter path. No new endpoint → no new
security surface. Update `docs/API_CONTRACTS.md` §2/§4.

## 7. Golden-set eval (mandatory before weighting baselined)
Create `tests/golden/expert_routing.jsonl` — 30–50 real prompts spanning:
telco proposal, Linux troubleshooting, M5 architecture, founder decision,
mixed-domain. Add a small eval script (pytest or plain unittest) that, per query,
records: route → retrieved memories → useful / leakage. Compares expert-routed
retrieval vs current global retrieval. Report route accuracy + leakage; do NOT
hardcode a passing number.

## Acceptance criteria (5 — all required)
1. **Additive schema** — `experts`/`expert_confidence` columns + `expert_axes`
   rules + identity keys; RC1 314 tests green; existing stores open w/o re-migration.
2. **Deterministic expert tagging + confidence** — curator emits `experts` list +
   `expert_confidence` dict; cto/founder gated to Experience; reproducible.
3. **Per-lens retrieval/blending under token budget** — `route_experts` shapes
   candidate generation; digest respects `user_char_limit`/`memory_char_limit` +
   `token_budget`; falls back to global when no axis fires.
4. **Golden-set eval** — expert routing beats global retrieval on the set (route
   accuracy up, leakage down); numbers reported, not asserted.
5. **Config-only tunability** — add a NEW axis `cloud_arch` purely via config
   (`expert_axes` + identity `persona.expert_axes`); runtime routes + tags it with
   NO code change; RC1 tests still green; golden re-run confirms pickup.

## Verification (Jebat re-runs ALL of this)
1. `python3 -m unittest discover -s tests` → all green, count = 314 + new.
2. `python3 -W error::ResourceWarning -m unittest discover -s tests` → zero
   ResourceWarning (do not regress Task6).
3. Unit test: adding row with `experts`/`expert_confidence` round-trips; NULL rows
   still retrieve as "no axis"; embedding-identity guard still refuses mixed vectors.
4. Unit test: `score_experts` on a telco segment returns `telco_presales` high +
   `expert_confidence` dict; cto/founder NOT attached to a knowledge segment.
5. Unit test: `route_experts` over a Linux prompt returns `linux_pro` dominant;
   `retrieve()` with no axis fires falls back to global (result set non-empty).
6. `expert_routing` present in `/v1/context/build` response for a sample prompt.
7. Golden eval script runs; prints route-accuracy + leakage for routed vs global.
8. **Tunability test (criterion 5):** add `cloud_arch` via config only, restart
   serve, confirm `/v1/context/build` for a cloud prompt shows `cloud_arch` in
   `expert_routing` and tags a freshly ingested cloud memory; RC1 tests still green.
9. `bash run_cortex.sh --from-hermes` → `~/.hermes/state.db` UNCHANGED (sha256).

## Report format
Files changed; test counts (314 + N, all green, zero ResourceWarning); the
`expert_routing` sample output; golden eval numbers (routed vs global); config-only
tunability evidence (criterion 5). Do NOT claim done without the golden-set numbers
and the tunability proof. No commit/push — Jebat validates first.
