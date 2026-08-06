# Jebat-Cortex

Automated memory pipeline modelling the **prefrontal cortex** — long-term
memory, context indexing, and information management for Jebat / Hermes.

Jebat-Cortex turns a conversation log into two memory types and merges them
into a hard-budgeted context block that is injected into Jebat's system prompt.

```
Chat -> Conversation -> Memory Curator -> (split)
  |- Experience  -> Graph Store   (relational / code graph; ripgrep fallback)
  |- Knowledge   -> Vector Store  (semantic long-term memory)
Both merge at Context Builder -> injected into Jebat system prompt.
```

## Why Graphify sits beside Jebat-Cortex

```
Jebat/Hermes  ->  Graphify (repo knowledge graph)  ->  Lekiu / Codex / Claude Code
Jebat-Cortex  ->  Experience (graph) + Knowledge (vector) memory
```

- **Jebat-Cortex** = the agent's own memory (what was said, what was learned).
- **Graphify** (Graphify-Labs/graphify, Apache-2.0) = the *repo brain* — a
  local, queryable knowledge graph of the codebase, so delegated coding agents
  (Lekiu) get structured repo context instead of coding blind.

Graphify is **not** a replacement for Jebat-Cortex; it is a complementary
structural-knowledge layer. Graphify is strong at repo/document structure, not
full conversational memory.

## Repository layout

```
core/        memory_curator.py, vector_store.py, graph_store.py, context_builder.py,
             rules.py, ingest_hermes.py
config/      cortex_rules.json
tests/       unit + fixture based
data/        sample conversation logs + generated stores (gitignored)
run_cortex.sh
```

## Quick start

```bash
# 1. Ingest the real Hermes corpus (read-only against ~/.hermes/state.db)
bash run_cortex.sh --from-hermes "what embedding backend does Jebat-Cortex use?"

# 2. Or ingest a sample conversation log
bash run_cortex.sh sample_conversation.jsonl "your question here"

# 3. Run the test suite
python3 -m unittest discover -s tests
```

Constraints (see `config/cortex_rules.json`):
- `user_char_limit` = 1375, `memory_char_limit` = 2200.
- `~/.hermes/state.db` is opened **read-only** — never written.
- No secrets, no credentials, no `.env` edits.

## Graphify repo-brain layer (local install)

Graphify is installed in an **isolated venv** (per directive — no blind install,
system Python 3.9.6 is too old; graphifyy needs >=3.10).

```bash
# One-time setup (already done on this host)
python3.11 -m venv .venv-graphify
. .venv-graphify/bin/activate
pip install graphifyy          # note the double-y package name
ln -s "$(pwd)/.venv-graphify/bin/graphify" "$HOME/.hermes/bin/graphify"
```

Build + query a repo graph (code-only, **no LLM key required**):

```bash
graphify extract "$PWD" --code-only --no-viz   # -> graphify-out/graph.json
graphify query "how does the vector store rank results?"
```

`graphify_context.sh` (in the `delegate_coding_to_lekiu` skill) wraps this and
is already wired into `lekiu-task`, so Lekiu receives structured codebase
context automatically. If Graphify is absent, it falls back to ripgrep/git.

> Generated artifacts `graphify-out/` and `.venv-graphify/` are gitignored.

## Verification status

- 112 unit tests green.
- Real `--from-hermes` run proves `~/.hermes/state.db` is never mutated.
- Graphify extract/query verified locally (389 nodes / 855 edges on this repo).

## Version history

- **v0.1** — prefrontal memory pipeline scaffold (54 tests).
- **v0.2** — real Hermes corpus ingestion + pluggable embedder (97 tests).
- **v0.3** — review hardening: tool/thinking payload filtering + full embedding
  identity enforcement (backend + model + dimension) with loud retrieval
  failure on mismatch (112 tests). Plus the Graphify repo-brain layer.

Roles: Jebat/Hermes = Architect & Memory Curator; Lekiu (Claude Code) = Builder;
Faisal = final approval authority.
