# Jebat-Cortex — Architecture

This document describes the internal structure of Jebat-Cortex and how it
combines with the Graphify repo-brain layer in Jebat's cognitive stack.

## 1. The cognitive stack

```
                         ┌─────────────────────────────────────┐
                         │         Jebat / Hermes              │
                         │   (Architect & Memory Curator)      │
                         └───────────────┬─────────────────────┘
                                         │ delegates coding work
                         ┌───────────────┴─────────────────────┐
                         │   Graphify (repo knowledge graph)   │  ← structural
                         │   graphify-out/graph.json (local)   │     context
                         └───────────────┬─────────────────────┘
                                         │ injected into prompt
                         ┌───────────────┴─────────────────────┐
                         │   Lekiu / Codex / Claude Code       │  ← builder
                         └─────────────────────────────────────┘

   Jebat's OWN memory (what was said / learned):
                         ┌─────────────────────────────────────┐
                         │   Jebat-Cortex memory pipeline      │
                         │   Experience (graph) + Knowledge    │
                         │   (vector)  ──► Context Builder     │
                         └─────────────────────────────────────┘
```

Graphify answers "what is the structure of this codebase?". Jebat-Cortex answers
"what do we know from past conversations?". They are complementary; Graphify is
not a substitute for conversational memory.

## 2. Jebat-Cortex pipeline

```
Conversation log (JSONL)
        │
        ▼
Memory Curator  (core/memory_curator.py)
   - segment_message
   - classify  -> Knowledge | Experience
   - extract_relations (Experience edges)
   - embed     -> vector (Knowledge)
        │
        ├──────────────►  Graph Store    (core/graph_store.py)
        │                 nodes + edges, ripgrep fallback
        │
        └──────────────►  Vector Store   (core/vector_store.py)
                          semantic long-term memory
        │
        ▼
Context Builder (core/context_builder.py)
   - retrieve(prompt)  -> query both stores
   - merge + rank
   - apply_budget (hard 1375 / 2200 limits)
   - to_markdown / JSON digest
        │
        ▼
Injected into Jebat system prompt
```

### Roles
- **Jebat / Hermes** — Architect & Memory Curator (orchestrates, validates).
- **Lekiu (Claude Code)** — Builder (writes the codebase).
- **Faisal** — final approval authority (no commit/push/deploy without sign-off).

## 3. Ingestion sources

- `core/ingest_hermes.py` — reads the real Hermes corpus from
  `~/.hermes/state.db` **read-only** (ATTACH, never written). Filters tool noise
  and empty rows; converts to the JSONL the curator understands.
- `run_cortex.sh --from-hermes` runs the full read-only pipeline against the real
  DB. `state.db` size is asserted unchanged before/after as a read-only proof.

## 4. Embedding identity (v0.3 hardening)

A vector store row carries a full **embedding identity**:

```
embed_meta = { "backend": str, "model": str, "dim": int }
```

This prevents silent corruption when the embedding configuration changes:

- `VectorStore.add()` **refuses** (raises `ValueError`) any row whose identity
  differs from the store's established identity.
- `VectorStore.query()` **skips** rows whose full identity ≠ the active embedder
  (not just dimension — a same-dimension model swap is also detected).
- `ContextBuilder.retrieve()` calls `vector.check_compatibility(
  raise_on_mismatch=True)` **before** querying, so production retrieval **fails
  loud** (not silent) when the store was built with a different backend/model/dim.
- The store captures its identity at open time; a pre-existing *mixed* store
  fails at open rather than being silently "fixed".

Why this matters: a `deterministic -> sentence-transformers` flip (or one ST
model for another at the same dimension) would otherwise score old rows as 0.0
and drop them from retrieval with no error — exactly the silent memory-loss
failure v0.3 closes.

## 5. Graphify repo-brain layer

- Installed in an **isolated venv** (`.venv-graphify/`, Python 3.11) — the
  package is `graphifyy` (double-y), not `graphify`. System Python 3.9.6 is too
  old.
- `graphify extract "$PWD" --code-only --no-viz` builds `graphify-out/graph.json`
  from the code AST **locally, no LLM key**. Non-code files need an API key;
  `--code-only` skips them.
- `graphify query "<question>"` traverses the graph (BFS/DFS) and returns the
  relevant nodes/edges/communities.
- `core/graph_store.py` (Jebat-Cortex's *own* experience graph) is separate from
  Graphify; it uses ripgrep as fallback when no graph DB is present.
- `graphify_context.sh` (delegate_coding_to_lekiu skill) wraps extract+query and
  is wired into `lekiu-task`, so delegated coding agents receive structured repo
  context. Falls back to ripgrep/git inspection if Graphify is absent.

Both `graphify-out/` and `.venv-graphify/` are gitignored.

## 6. Verification contract

- `python3 -m unittest discover -s tests` — 112 tests, must stay green.
- Real `--from-hermes` run must leave `~/.hermes/state.db` byte-for-byte unchanged.
- Graphify extract/query verified locally on this repo (389 nodes / 855 edges).
- All changes are committed only after independent verification; no push without
  Faisal's approval.
