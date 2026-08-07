# Deployment Runbook — Jebat-Cortex Semantic RAG on Proxmox (Tier A)

This runbook covers standing up Jebat-Cortex v1.1 (semantic RAG) on a fresh
Proxmox VM (no GPU). It captures the environment traps found during the
local semantic flip so the Proxmox box does not repeat them.

## 1. VM spec (Tier A, no GPU)
- 4–8 vCPU, 16–32 GB RAM, 256 GB+ NVMe.
- Ubuntu 22.04/24.04 (or Debian 12). Python 3.11 from the distro or pyenv.
- **Do NOT rely on the OS system python3 if it is < 3.10** — sentence-transformers
  3.x requires Python 3.10+. Use a dedicated venv on 3.11.

## 2. Provision the semantic venv (this is the trap we hit)
```bash
# on the Proxmox VM
python3.11 -m venv .venv-cortex-st
. .venv-cortex-st/bin/activate
pip install --upgrade pip
pip install "sentence-transformers"        # CPU build pulls torch CPU automatically
python -c "import sentence_transformers as s; print(s.__version__)"  # must import
```
- The model `paraphrase-multilingual-MiniLM-L12-v2` (384d, ~470 MB) downloads on
  first use and caches under `~/.cache/huggingface/`. Pre-warm it once:
  `python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')"`
- Cortex's `run_cortex.sh` auto-selects `.venv-cortex-st/bin/python` when present,
  so the same script works on both the dev box and Proxmox.

## 3. Configure
`config/cortex_rules.json` already ships the semantic config:
```json
"embedding": {
  "backend": "sentence-transformers",
  "model": "paraphrase-multilingual-MiniLM-L12-v2",
  "fallback_dimensions": 512,
  "fallback_ngram_size": 4,
  "fallback_ngram_weight": 0.35
}
```
- If the venv/model is missing, `get_embedder` now **raises** (fail-loud), so the
  service will not silently serve lexical recall. To allow a dev-box degrade,
  call `get_embedder(rules, allow_fallback=True)` — not the default path.
- `retrieval.min_score` is set to **0.42** (measured: semantic top-5 neighbours
  sit at 0.44–0.72, global median 0.11). Re-tune with `analyze_min_score.py`
  if you swap the model.

## 4. First ingest (read-only from Hermes)
```bash
bash run_cortex.sh --from-hermes
```
- Opens `~/.hermes/state.db` **read-only** (`mode=ro`). Cortex never writes it.
- Builds a FRESH `data/vector_store.db` (semantic). Old lexical stores cannot mix
  with semantic vectors — the identity guard refuses and you must start clean.
- Re-ingest regenerates expert-axis tags automatically (Curator attaches them at
  ingest), so the MoE lens metadata survives the rebuild.
- 24k rows on CPU takes ~8–10 min. Run it once; the store is then persistent.

## 5. Run the service
```bash
python3 -m cli.cortex_cli serve --port 8765 &
python3 -m cli.cortex_cli worker &
curl -s http://127.0.0.1:8765/v1/health    # expect embedding_identity: sentence-transformers/.../384
```
- For production use the launchd/systemd units (two jobs: serve + worker).
- Bind is 127.0.0.1 only. No public exposure.

## 6. Verify after deploy
- `curl /v1/health` → `embedding_identity` shows `sentence-transformers`,
  `paraphrase-multilingual-MiniLM-L12-v2`, `dim: 384`.
- `python3 -m unittest discover -s tests` → green.
- Golden-set eval (`tests/test_expert_routing_eval.py`) → routed precision
  should be ~63% (was 45% lexical), irrelevant ~36% (was 54%).
- Read-only proof: `state.db` sha256 unchanged across a `--from-hermes` run.

## 7. Gotchas (learned the hard way)
1. **Python version**: system python3 may be 3.9 (Apple/Debian). ST needs 3.10+.
   Always use the venv. Don't `pip install` against a newer python that isn't the
   one Cortex runs under — the import will fail on the real interpreter.
2. **venv pip bootstrap**: on some macOS/limited boxes `ensurepip` resolves to a
   broken external pip. Build the venv from a known-good 3.11 interpreter.
3. **Re-ingest is mandatory** on backend flip — you cannot in-place convert a
   lexical store. Back up the old db first.
4. **min_score is model-specific** — never carry the lexical 0.02 forward.
5. **CPU-only is fine for query** (ms) but **slow for bulk ingest** — do it once
   at provisioning, not per request.
