# TASK 8 — Semantic Embedder Flip + Re-ingest (v1.1 RAG intelligence layer)

You are **Lekiu** (Claude Code), Builder under Jebat/Hermes supervision.
This flips Jebat-Cortex's RAG from lexical (deterministic dim-512) to semantic
(sentence-transformers), making memory recall understand meaning instead of
keyword overlap. Source of truth: this file + `core/rules.py`, `config/cortex_rules.json`,
`core/vector_store.py`, `core/memory_curator.py`, `run_cortex.sh`.

## Context
RC1→GA shipped (v1.0.0). Expert-Axis Routing v0.5 shipped (MoE memory layer).
The ONE remaining gap: the vector store is still lexical (hashed bag-of-words).
Paraphrase/meaning recall is weak. This task installs a semantic embedder and
re-ingests the real Hermes corpus (24,200 rows) so RAG becomes semantic. This is
the "intelligence layer" for recall; MoE expert-axis routing stays keyword-based
(score_axes uses keyword_overlap, not the embedder) — that is intentional and
unchanged.

## Hard constraints (SECURITY_MODEL + repo CLAUDE.md)
- `~/.hermes/state.db` read-only (never opened writable). Re-ingest reads it, never writes.
- Localhost only; no shell exec through API/MCP; no commit/push/merge/deploy.
- Keep existing tests green. Python 3.9+.
- Embedding-identity guard (`check_compatibility`) MUST stay intact — re-ingest
  uses a FRESH db because the old lexical store cannot mix with semantic vectors.
- Fail closed: a configured-but-unavailable semantic backend must RAISE, not warn+fallback.

## Problems being fixed (see Jebat plan)
- P1: get_embedder silent-degrades to deterministic on ST failure → make it fail-loud
  when backend explicitly set to sentence-transformers.
- P3: min_score (0.02) + vector_top_k (5) tuned for lexical cosine → retune on the
  new semantic distribution (measure, don't guess).
- P4: model must be multilingual (EN+MS corpus) → paraphrase-multilingual-MiniLM-L12-v2 (384d).
- P2: 24,200 existing rows → full re-ingest from Hermes (read-only), expert tags
  regenerated automatically by Curator.expert_tags at ingest.

## Required Work
1. Install sentence-transformers (CPU-only) in the project venv; download the
   multilingual model. Verify `import sentence_transformers` works.
2. `core/rules.py` `get_embedder()`: when `backend == "sentence-transformers"` and
   `SentenceTransformerEmbedder` raises `EmbedderUnavailableError`, DO NOT fall back
   to deterministic — re-raise (fail-loud). Keep deterministic-as-default behaviour
   for `backend == "deterministic"/""` unchanged. Add a unit test proving the
   configured-ST-but-missing case raises instead of degrading.
3. `config/cortex_rules.json`: set `embedding.backend = "sentence-transformers"`,
   `embedding.model = "paraphrase-multilingual-MiniLM-L12-v2"`, remove the
   `dimensions`/`ngram_*` lexical keys (model defines dim). Keep `retrieval` block.
4. Re-ingest: back up old `data/vector_store.db` (rename to `vector_store.lexical.bak`),
   let `run_cortex.sh --from-hermes` build a fresh semantic store. Confirm
   `~/.hermes/state.db` is byte-unchanged (sha256 pre/post).
5. Retune `retrieval.min_score`: sample query→memory cosine on the new store, pick a
   floor that keeps precision high (e.g. measure 5th-percentile similarity of known-
   relevant pairs). Update `cortex_rules.json` + document the chosen value.
6. Re-run `tests/test_expert_routing_eval.py` golden set — confirm semantic routing
   still beats global (route accuracy up, leakage 0). Expert tags must still appear.
7. New/updated tests: (a) semantic backend fail-loud test; (b) embedding identity
   reports sentence-transformers/multilingual/384 after flip; (c) re-ingest
   round-trips expert tags (a telco memory re-tagged telco_presales); (d) tunability
   (cloud_arch via config) still works post-flip.

## Do Not Change
- Do NOT modify `~/.hermes/state.db`.
- Do NOT rewrite retrieval logic destructively.
- Do NOT change MoE score_axes (stays keyword-based — by design).
- Preserve pre-existing uncommitted work (CLAUDE.md, test_close_discipline.py).
- Do NOT commit/push/merge/deploy (Jebat does that after validation).

## Acceptance Criteria
1. Semantic embedder installed + configured; get_embedder raises (not warns) when
   ST configured-but-unavailable.
2. Fresh semantic vector store built from Hermes (24k+ rows); old lexical DB backed up.
3. Embedding identity = sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/384.
4. min_score retuned on measured semantic distribution (value documented, not guessed).
5. Expert tags preserved through re-ingest; MoE routing + tunability still work.
6. Golden-set eval: semantic routed retrieval beats global; state.db unchanged.
7. All existing tests + new tests green; zero ResourceWarning.

## Verification (Jebat re-runs ALL)
1. `python3 -m unittest discover -s tests` → green.
2. `python3 -W error::ResourceWarning -m unittest discover -s tests` → zero warnings.
3. `curl -s http://127.0.0.1:8765/v1/health | python3 -m json.tool` →
   embedding_identity shows sentence-transformers + multilingual + 384.
4. `bash run_cortex.sh --from-hermes` → state.db sha256 unchanged.
5. Golden eval numbers printed (routed vs global).
6. `python3 -c "from core.rules import get_embedder, load_rules; e=get_embedder(load_rules()); print(e.name, e.model, e.dimensions)"` → sentence-transformers paraphrase-multilingual-MiniLM-L12-v2 384.
