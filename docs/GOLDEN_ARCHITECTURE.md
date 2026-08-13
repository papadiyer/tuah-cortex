# Jebat-Cortex — GOLDEN Architecture (canonical reference)

**Status:** Adopted as the golden baseline on 2026-08-08.
**Authority:** Supersedes any ad-hoc mental model until a *reviewed and verified*
improvement replaces it. Changes require passing tests + Faisal sign-off
(no commit/push/deploy without approval).

**Source of truth:** derived from verified code
(`core/memory_curator.py`, `core/context_builder.py`, `core/graph_store.py`,
`core/vector_store.py`) and `ARCHITECTURE.md`. Confirmed live at write time:
Cortex `GET /v1/health` → `status: healthy`, both stores `ready`.

---

## 1. Golden flow (verified working)

```
Pengguna input arahan
        │
        ▼
Memory Curator  (core/memory_curator.py)
   - segment_message
   - classify  →  Knowledge  |  Experience
   - extract_relations  →  triples  (Experience: subj–rel–obj)
   - embed               →  vectors  (Knowledge)
        │
        ├──────────────►  Graph Store    (core/graph_store.py)
        │                 Experience: nodes + edges, ripgrep fallback
        │
        └──────────────►  Vector Store   (core/vector_store.py)
                          Knowledge: semantic long-term memory
        │
        ▼
Context Builder  (core/context_builder.py)
   - retrieve(prompt)  →  query BOTH stores
   - merge + rank      (Knowledge w=1.0, Experience w=0.9)
   - apply_budget      (HARD limit: prompt 1375 / memory 2200 char)
   - to_markdown + JSON digest   ← the JSON/Triples OUTPUT contract
        │
        ▼
Injected into Jebat / Hermes system prompt
        │
        ▼
Jebat / Hermes "Brain"  — reasons using the injected context
```

**Embedder identity (hardened, v0.3):**
`paraphrase-multilingual-MiniLM-L12-v2` (sentence-transformers, 384-dim),
or `DeterministicEmbedder` fallback. Each store row carries a full embed
identity `{backend, model, dim}`; `VectorStore` refuses mismatched rows and
`ContextBuilder.retrieve()` fails **loud** on identity mismatch — no silent
memory loss.

---

## 2. Why this is golden (what it guarantees)

- **Dual-store separation** models the prefrontal split: Experience (episodic /
  relational triples) vs Knowledge (semantic vectors). Classification happens
  *before* embedding, so each memory type is routed to the correct store.
- **Retrieval + merge + rank** is the core value-add: relevant past context is
  pulled per prompt, not blindly formatted.
- **Hard budget enforcement** (1375 / 2200) keeps the injected block safe and
  predictable — the safety rail, not optional polish.
- **Embedder identity hardening** prevents silent corruption when the embedding
  backend/model changes.
- **Localhost-only + read-only Hermes ingest** (`~/.hermes/state.db` ATTACH,
  size asserted unchanged) keeps the system safe.

---

## 3. Evaluated alternative (user-suggested flow — NOT adopted)

User's proposed flow:

```
Pengguna input arahan
   │
   ▼
Jebat cortex semantic embedder
   │
   ▼
Graphify engine
   │
   ▼
Format JSON/Triples
   │
   ▼
Hermes/Jebat Brain
```

**Assessment: rejected as golden.** Three concrete flaws vs the verified flow:

1. **Embed before classify.** The proposed flow embeds raw input at stage 2.
   In the golden flow, embedding happens *inside* the Memory Curator **after**
   `classify` — only the Knowledge branch is embedded; Experience becomes
   triples. Embedding-first destroys the dual-store routing that gives the
   system its prefrontal model.

2. **Graphify inline in the hot path (wrong layer).** Graphify
   (`graphifyy`, isolated `.venv-graphify/`) is a *repo code-knowledge graph*
   for **delegated coding** (feeds Lekiu/Codex, not Jebat's reasoning). It runs
   a full AST scan (`graphify extract`) and is irrelevant + expensive to invoke
   on every conversational turn. The golden flow keeps Graphify as a **sibling
   layer** feeding the builder, never between input and brain.

3. **Drops retrieval / merge / budget.** The proposed flow goes straight from
   Graphify to "Format JSON/Triples" with no `Context Builder` retrieve+merge+
   budget stage. Without retrieval, relevant past memory is never pulled;
   without the budget, injection is unbounded. This removes the system's core
   value-add and its safety rail in one step.

**Valid intuitions preserved from the suggestion:**
- *"JSON/Triples"* is correctly understood as the **output contract** of the
  Context Builder (not a processing stage) — captured in §4.
- *Graphify* is recognised as a real, wired layer — correctly placed as a
  **sibling** feeding delegated coding, documented in §5.

---

## 4. Output contract (JSON / Triples)

- **Knowledge** → semantic vectors in Vector Store; surfaced as ranked Markdown
  blocks in the context digest.
- **Experience** → typed triples `(subj, rel, obj)` in Graph Store; surfaced via
  keyword query with ripgrep fallback when the graph lacks a keyword.
- **Context Builder emits two synchronized views**, both under the hard budget:
  - `JSON digest` — machine-readable (retrieval provenance, scores, budget).
  - `Markdown` — injected into the Jebat system prompt as working memory.

---

## 5. Sibling layer — Graphify (not in Jebat's reasoning path)

```
Graphify (code knowledge graph, package "graphifyy")
   graphify extract "$PWD" --code-only → graphify-out/graph.json (local AST, NO LLM key)
        │ injected into prompt
        ▼
   Lekiu / Codex / Claude Code   ← builder, not Jebat
```

`graphify_context.sh` (delegate_coding_to_lekiu) wraps extract+query and is
wired into `lekiu-task`, so delegated coding agents get structured repo context.
Falls back to ripgrep/git inspection if Graphify is absent. `core/graph_store.py`
(Jebat-Cortex's own Experience graph) is **separate** from Graphify.

> Graphify answers *"what is the structure of this codebase?"*. Jebat-Cortex
> answers *"what do we know from past conversations?"*. Complementary; Graphify
> is not a substitute for conversational memory.

---

## 6. When to revise the golden

A future change may replace this baseline only if ALL hold:
- verified by a benchmark / golden-set eval showing better retrieval or safety;
- passes the existing test suite (vector-store identity, budget enforcement,
  graph-store relations, ingest read-only contract);
- Faisal approves (no commit/push/deploy without sign-off).

Candidate future promotions (require eval, not assumed):
- Graphify promoted to an **optional in-path context source** for code-heavy
  turns (needs relevance + cost proof).
- A new embedder backend (must preserve the identity-hardening contract).
