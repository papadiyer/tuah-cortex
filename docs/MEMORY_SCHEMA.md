# Memory Schema — Provenance, Status, Tiers (GA / v1.0.0)

Design doc for Task2 Workstream 3 (Memory Policy / tiers) and Workstream 4
(Provenance and Memory Status). Extends the existing vector/graph stores
**additively** — no rewrite of `core/vector_store.py` / `core/graph_store.py`.

## 1. Provenance & status (every memory)

Minimum fields (Task2 Workstream 4):

```json
{
  "memory_id": "stable-id",
  "type": "decision | experience | knowledge | task | artefact",
  "status": "proposed | approved | rejected | completed | superseded",
  "source_type": "conversation | document | hermes_db | agent_event",
  "source_id": "source identifier",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "confidence": 0.0,
  "project": "optional",
  "entities": []
}
```

- Stored as the `meta` JSON column already present in `knowledge` and as a new
  `meta` column on graph edges/nodes (additive migration).
- `status` is explicit. **A proposal is never presented as an approved
  decision.** Retrieval filters default to `status='approved'` for decisions
  unless `approved_only=false`.
- `type` drives routing: `knowledge` → vector store, `experience` → graph store,
  `decision` / `task` / `artefact` → structured tables (new, lightweight).

## 2. Memory tiers (retrieval policy)

**Tier 0 — Always Loaded** (stable config/structured memory, NOT semantic):
- Faisal authority + approval/permission policy.
- M5 role definitions.
- Jebat identity.
- Active project (when reliably resolved).
- Critical operating rules (the `cortex_rules.json` limits, read-only policy).

Source: `config/` + a small `identity` table — never depends on cosine recall.

**Tier 1 — Retrieved for Current Task** (semantic + structured lookup):
- latest approved decisions;
- recent relevant experiences (graph);
- unresolved tasks;
- relevant people/agents;
- related artefacts;
- project status;
- lessons learned.

Source: vector store (knowledge) + graph store (experience) + decision/task
tables, scoped by `project` + `entities` from the prompt.

**Tier 2 — On Demand** (NOT auto-injected):
- complete conversations;
- old documents;
- historical detail;
- large source artefacts;
- repo-specific technical detail (Graphify graph lives here, separately).

Exposed only via `/v1/memory/search` when explicitly queried.

## 3. Status lifecycle

```
proposed -> approved | rejected
approved -> superseded   (when a newer decision replaces it)
completed               (tasks/artefacts)
```
Superseded memories remain queryable for provenance but are excluded from
default retrieval. Never hard-deleted by the runtime.

## 4. Graphify separation (Workstream 7)

Graphify context is **Tier 2** technical detail. It is included in the context
digest only when:
- a repository is identified (`project_hint` / resolved project has a path);
- repo context is relevant to the prompt;
- `graphify-out/graph.json` exists and is valid;
- token budget permits.

Its provenance is recorded **separately** (`source_type: graphify_graph`), and
it is never merged into the conversational experience graph without a deliberate
mapping layer (Task2 Non-Goal).

## 5. Files (Phase 3)

- `core/vector_store.py` — add `provenance`/`status` to `meta` (additive).
- `core/graph_store.py` — add `meta` column to nodes/edges (additive).
- `core/memory_curator.py` — emit provenance on ingest; `ingest_event()` sets
  status from the postflight event.
- `core/context_builder.py` — `build_context()` returns tiered payload
  (Tier 0 from config, Tier 1 retrieved, Tier 2 omitted) + `provenance[]`.
- `api/context.py` — maps `build_context()` to the `/v1/context/build` response.
