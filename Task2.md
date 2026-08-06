# TASK — Build Jebat Cognitive Runtime Integration for M5

## Role

You are **Jebat**, Architect, Builder Lead and Memory Curator for The Magnificent 5.

You will design, delegate and validate the implementation of the next Jebat-Cortex milestone.

Use **Lekiu / Claude Code / Codex** for coding work where appropriate, but Jebat remains responsible for architecture, task decomposition, review, testing and final reporting.

Faisal is the final approval authority.

Do not commit, push, deploy or modify production Hermes data without Faisal's explicit approval.

---

# Objective

Transform `jebat-cortex` from a standalone conversational-memory pipeline into a persistent local cognitive runtime for M5.

The runtime must:

1. Stay available as a long-running local service.
    
2. Receive every M5 request through Tuah.
    
3. Build relevant context before Tuah or any M5 agent starts reasoning.
    
4. Provide additional memory retrieval to Jebat, Lekiu and other agents through MCP.
    
5. Record completed work, decisions, lessons and artefacts after execution.
    
6. Preserve strict read-only handling of the original Hermes `state.db`.
    
7. Remain modular, testable, observable and reversible.
    

Working repository:

```text
~/dev/projects/the-magnificent-5/jebat-cortex
```

Review the existing repository and especially:

```text
docs/ARCHITECTURE.md
core/memory_curator.py
core/context_builder.py
core/graph_store.py
core/vector_store.py
core/ingest_hermes.py
run_cortex.sh
```

Do not replace the existing architecture blindly.

Extend and harden it.

---

# Target Architecture

```text
Faisal
  │
  ▼
Tuah UI / Voice
  │
  ▼
M5 Request Gateway
  │
  ├── Mandatory context preflight
  │       ▼
  │   Jebat Cognitive Runtime
  │       ├── Identity memory
  │       ├── Active project state
  │       ├── Experience graph
  │       ├── Knowledge vector store
  │       ├── Decision history
  │       ├── Open tasks
  │       └── Context Builder
  │
  ▼
Tuah orchestration
  │
  ├── Jebat
  ├── Kasturi
  ├── Lekir
  └── Lekiu
          │
          ├── Jebat-Cortex MCP
          └── Graphify repo context
  │
  ▼
Execution result
  │
  ▼
Postflight memory event
  │
  ▼
Curator → classify → extract → store → consolidate
```

---

# Core Design Decision

Do not spawn a fresh Python process for every Jebat request as the primary production flow.

The preferred architecture is:

```text
Persistent local Cortex service
        +
Internal HTTP API for Tuah
        +
MCP adapter for specialist agents
        +
CLI fallback for debugging and recovery
```

The LLM must not decide whether memory retrieval is required.

Every request entering through Tuah must execute a mandatory context preflight.

---

# Workstream 1 — Repository Assessment

Before coding:

1. Inspect the current codebase.
    
2. Identify reusable modules.
    
3. Identify missing interfaces.
    
4. Identify technical debt or conflicting assumptions.
    
5. Confirm how the vector store, graph store and embedding identity currently work.
    
6. Confirm how Hermes `state.db` is accessed and protected.
    
7. Produce:
    

```text
docs/COGNITIVE_RUNTIME_ASSESSMENT.md
```

The assessment must include:

- current architecture;
    
- reusable components;
    
- gaps;
    
- risks;
    
- migration approach;
    
- files proposed for creation or modification;
    
- explicit non-goals.
    

Do not begin large-scale refactoring until this assessment is complete.

---

# Workstream 2 — Persistent Runtime Service

Build a local long-running service.

Preferred implementation:

```text
FastAPI or another lightweight Python ASGI framework
```

Suggested structure:

```text
jebat-cortex/
├── api/
│   ├── app.py
│   ├── models.py
│   ├── context.py
│   ├── memory.py
│   ├── events.py
│   ├── health.py
│   └── admin.py
├── core/
├── mcp/
│   └── server.py
├── workers/
│   ├── ingest_worker.py
│   ├── consolidation_worker.py
│   └── queue.py
├── service/
│   ├── launchd/
│   └── docker/
├── cli/
│   └── cortex_cli.py
└── tests/
```

Adapt this structure where the current repository suggests a better arrangement.

Do not reorganize files unnecessarily.

---

# Required API Endpoints

## 1. Health

```http
GET /v1/health
```

Return:

- service state;
    
- graph-store state;
    
- vector-store state;
    
- embedding identity;
    
- queue state;
    
- last successful ingestion;
    
- version;
    
- uptime.
    

Example:

```json
{
  "status": "healthy",
  "version": "0.4.0",
  "graph_store": "ready",
  "vector_store": "ready",
  "embedding_identity": "model-name-or-hash",
  "queue_depth": 0,
  "last_ingestion": "ISO-8601 timestamp"
}
```

## 2. Context Preflight

```http
POST /v1/context/build
```

Input model:

```json
{
  "request_id": "uuid",
  "actor": "faisal",
  "entrypoint": "tuah",
  "prompt": "user request",
  "project_hint": "optional",
  "session_id": "optional",
  "active_workspace": "optional",
  "token_budget": 2200,
  "permissions": {
    "read_memory": true,
    "write_memory": true,
    "execute": false
  }
}
```

Return:

```json
{
  "request_id": "uuid",
  "resolved_project": "optional",
  "identity": {},
  "active_projects": [],
  "relevant_experiences": [],
  "relevant_knowledge": [],
  "relevant_decisions": [],
  "open_tasks": [],
  "relations": [],
  "recommended_agent": "jebat",
  "context_markdown": "budgeted context digest",
  "provenance": [],
  "warnings": []
}
```

The response must remain within the requested context budget.

## 3. Postflight Event

```http
POST /v1/events/postflight
```

Input:

```json
{
  "request_id": "uuid",
  "session_id": "optional",
  "actor": "faisal",
  "agent": "jebat",
  "project": "optional",
  "prompt": "original request",
  "result_summary": "completed result",
  "status": "completed",
  "decisions": [],
  "lessons": [],
  "open_tasks": [],
  "artefacts": [],
  "provenance": [],
  "timestamp": "ISO-8601"
}
```

This endpoint must persist the event durably before returning success.

Do not rely on:

```bash
some_command &
```

as the primary ingestion mechanism.

## 4. Memory Search

```http
POST /v1/memory/search
```

Support filters for:

- project;
    
- entity;
    
- date range;
    
- memory type;
    
- status;
    
- confidence;
    
- source;
    
- approved decisions only.
    

## 5. Project State

```http
GET /v1/projects/{project_id}/state
```

Return:

- objective;
    
- current status;
    
- latest approved decisions;
    
- unresolved decisions;
    
- open tasks;
    
- latest artefacts;
    
- related agents;
    
- recent experiences.
    

---

# Workstream 3 — Memory Policy

Implement three retrieval tiers.

## Tier 0 — Always Loaded

- Faisal authority.
    
- M5 role definitions.
    
- approval and permission policy.
    
- Jebat identity.
    
- active project, where reliably resolved.
    
- critical operating rules.
    

Tier 0 must not depend entirely on semantic retrieval.

It should come from stable structured memory or configuration.

## Tier 1 — Retrieved for Current Task

- latest approved decisions;
    
- recent relevant experiences;
    
- unresolved tasks;
    
- relevant people and agents;
    
- related artefacts;
    
- project status;
    
- lessons learned.
    

## Tier 2 — On Demand

- complete conversations;
    
- old documents;
    
- historical detail;
    
- large source artefacts;
    
- repository-specific technical detail.
    

Do not inject Tier 2 automatically unless needed.

---

# Workstream 4 — Provenance and Memory Status

Every meaningful memory returned by Cortex must contain provenance.

Minimum fields:

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

The system must differentiate:

- factual record;
    
- proposal;
    
- approved decision;
    
- rejected decision;
    
- assumption;
    
- lesson learned;
    
- unresolved task;
    
- superseded memory.
    

Never present a proposal as an approved decision.

---

# Workstream 5 — Durable Event Queue

Build a lightweight durable queue suitable for local deployment.

Acceptable MVP options:

- SQLite-backed queue;
    
- existing durable local store already present in the repository;
    
- another simple dependency with clear justification.
    

Requirements:

- events survive process restart;
    
- retry with bounded backoff;
    
- dead-letter state;
    
- idempotency using `request_id`;
    
- observable queue depth;
    
- no duplicate ingestion;
    
- worker can restart safely.
    

Do not introduce Redis, Kafka, Temporal or another major dependency unless clearly justified.

Prefer the smallest reliable implementation.

---

# Workstream 6 — MCP Adapter

Expose Jebat-Cortex as a local MCP server.

Required tools or equivalent:

```text
cortex.search_memory
cortex.build_context
cortex.get_project_state
cortex.get_decision_history
cortex.get_related_experiences
cortex.record_outcome
cortex.health
```

MCP is primarily for:

- Jebat;
    
- Lekiu;
    
- Claude Code;
    
- Codex;
    
- future M5 specialists.
    

Tuah's mandatory preflight should use the internal API directly, not depend solely on optional MCP tool selection.

Document configuration examples for:

- Claude Code;
    
- Codex, where supported;
    
- generic stdio MCP client.
    

Do not expose destructive administration functions through MCP by default.

---

# Workstream 7 — Graphify Integration

Preserve the separation:

```text
Graphify:
"What is the structure of this codebase?"

Jebat-Cortex:
"What do we know from prior conversations and experience?"
```

Add a clean integration interface allowing Context Builder to include Graphify context only when:

- a repository is identified;
    
- repo context is relevant;
    
- Graphify output exists and is valid;
    
- token budget permits.
    

Do not merge Graphify's graph permanently into the conversational experience graph without a deliberate mapping layer.

Record Graphify provenance separately.

---

# Workstream 8 — Tuah Integration Contract

Create:

```text
docs/TUAH_CORTEX_INTEGRATION.md
```

Define the exact request lifecycle.

## Preflight

```text
Tuah receives Faisal request
→ generates request_id
→ calls POST /v1/context/build
→ receives context digest
→ injects digest into orchestration state
→ selects agent
→ executes task
```

## Postflight

```text
agent finishes
→ Tuah creates structured result event
→ calls POST /v1/events/postflight
→ Cortex persists event
→ worker curates memory
→ memory becomes available for future retrieval
```

Include:

- timeout policy;
    
- retry policy;
    
- degraded mode;
    
- permissions;
    
- error response format;
    
- idempotency;
    
- context budget;
    
- logging and correlation IDs.
    

## Degraded Mode

If Cortex is unavailable:

- Tuah may continue only with current-session context;
    
- clearly mark `memory_status=degraded`;
    
- do not fabricate memory;
    
- log the failure;
    
- do not perform destructive actions based on missing context;
    
- surface a warning to Faisal where material.
    

---

# Workstream 9 — Service Lifecycle

Provide local service definitions.

Primary target:

```text
macOS launchd
```

Optional secondary target:

```text
Docker Compose
```

Requirements:

- starts automatically;
    
- restarts on failure;
    
- binds only to localhost by default;
    
- logs to a controlled directory;
    
- configurable environment file;
    
- health-check command;
    
- clean shutdown;
    
- documented install and uninstall process.
    

Suggested local endpoint:

```text
http://127.0.0.1:8765
```

Do not bind publicly by default.

Create:

```text
service/launchd/com.m5.jebat-cortex.plist
docs/SERVICE_OPERATIONS.md
```

The plist must use actual configurable paths or a generated installation process.

Do not hard-code another user's home directory.

---

# Workstream 10 — Security

Mandatory requirements:

1. Hermes `~/.hermes/state.db` remains read-only.
    
2. Preserve the existing before-and-after size assertion.
    
3. Add file hash or stronger read-only verification if practical.
    
4. Bind API to localhost only.
    
5. No secrets in logs.
    
6. Redact tokens, API keys, cookies and credentials.
    
7. Validate input sizes.
    
8. Apply request timeout limits.
    
9. Restrict administrative endpoints.
    
10. No shell command execution through Cortex API.
    
11. No commit, push or deployment without Faisal's approval.
    
12. Add a clear threat-model section.
    

Create:

```text
docs/SECURITY_MODEL.md
```

---

# Workstream 11 — Observability

Implement structured logging.

Each request should include:

```text
request_id
session_id
actor
entrypoint
resolved_project
retrieval_count
context_tokens
duration_ms
memory_status
queue_status
error_code
```

Do not log full sensitive prompts by default.

Add metrics or a status summary for:

- context-build latency;
    
- graph retrieval latency;
    
- vector retrieval latency;
    
- number of memories returned;
    
- queue depth;
    
- failed jobs;
    
- duplicate events;
    
- last successful consolidation.
    

---

# Workstream 12 — Tests

Maintain all existing tests.

Add tests covering:

## Unit Tests

- API input validation;
    
- budget enforcement;
    
- memory tier selection;
    
- provenance generation;
    
- status handling;
    
- deduplication;
    
- idempotency;
    
- queue retries;
    
- queue restart recovery;
    
- Graphify optional injection;
    
- secret redaction.
    

## Integration Tests

- context preflight end-to-end;
    
- postflight persistence and ingestion;
    
- service restart with queued events;
    
- Hermes DB remains unchanged;
    
- MCP context retrieval;
    
- degraded mode;
    
- duplicate request submission.
    

## Acceptance Scenario

Use a representative test:

```text
Input:
"Tuah, sambung kerja Kasturi semalam."

Expected:
- resolves Kasturi-related project context;
- returns recent Kasturi decisions;
- returns Postiz-related recommendation if present;
- identifies unresolved tasks;
- does not inject unrelated Hermes conversations;
- remains within token budget;
- records provenance;
- recommends the correct agent;
- completes below the defined local latency threshold.
```

Choose a reasonable local latency target based on measured baseline.

Do not fabricate benchmark numbers.

Record actual results.

---

# Deliverables

Create or update:

```text
docs/COGNITIVE_RUNTIME_ASSESSMENT.md
docs/ARCHITECTURE.md
docs/TUAH_CORTEX_INTEGRATION.md
docs/SERVICE_OPERATIONS.md
docs/SECURITY_MODEL.md
docs/MCP_INTEGRATION.md
docs/TEST_REPORT.md
docs/RELEASE_NOTES_v0.4.md
```

Code deliverables should include:

```text
persistent local API service
durable event queue
postflight worker
MCP adapter
launchd service support
CLI fallback
tests
health checks
structured logging
```

---

# Non-Goals for This Milestone

Do not:

- replace CrewAI;
    
- build a new Tuah UI;
    
- modify Hermes production database;
    
- merge Graphify and Cortex into one undifferentiated graph;
    
- introduce heavyweight distributed infrastructure;
    
- allow autonomous commit, push or deployment;
    
- ingest every raw conversation into prompts;
    
- treat all memories as equally trusted;
    
- make Cortex publicly network-accessible;
    
- refactor unrelated working code.
    

---

# Implementation Principles

Follow these principles:

```text
Deterministic preflight before probabilistic reasoning.

Structured memory before raw chat retrieval.

Approved decisions before speculative suggestions.

Persistent service before per-request process spawning.

Durable event queue before shell background jobs.

Provenance before confidence.

Graceful degradation before silent failure.

Small dependable dependencies before infrastructure bloat.

Human approval before irreversible action.
```

---

# Required Execution Sequence

## Phase 1 — Assess

- inspect repository;
    
- run existing tests;
    
- produce architecture assessment;
    
- identify compatibility risks.
    

## Phase 2 — Design

- finalize service boundaries;
    
- define API contracts;
    
- define event schema;
    
- define memory schema and migration strategy;
    
- define MCP surface.
    

## Phase 3 — Build

Delegate implementation tasks to Lekiu where useful.

Keep tasks small and reviewable.

Suggested batches:

```text
Batch A — API models and health service
Batch B — Context preflight endpoint
Batch C — Durable queue and postflight event
Batch D — Workers and consolidation
Batch E — MCP adapter
Batch F — launchd lifecycle
Batch G — Tuah integration contract
Batch H — tests, security and observability
```

## Phase 4 — Validate

- run complete tests;
    
- conduct adversarial review;
    
- verify Hermes DB unchanged;
    
- verify localhost-only binding;
    
- verify restart recovery;
    
- verify context budget;
    
- verify no secret leakage;
    
- verify degraded mode;
    
- verify MCP tools.
    

## Phase 5 — Report

Return a Decision Package to Faisal.

---

# Final Report Format

Use this exact structure:

```markdown
# Jebat Cognitive Runtime — Build Report

## Executive Summary
What was built and whether the objective was achieved.

## Architecture Implemented
Service boundaries and request flow.

## Files Added
List with purpose.

## Files Modified
List with purpose.

## Tests
- existing tests;
- new tests;
- total passed;
- failures;
- skipped tests;
- actual latency results.

## Security Validation
- Hermes DB read-only proof;
- localhost binding;
- secret redaction;
- admin restrictions.

## Tuah Integration
Exact API and lifecycle.

## MCP Integration
Tools exposed and configuration.

## Known Limitations
Honest remaining limitations.

## Risks
Current operational and architectural risks.

## Rollback
How to disable or revert safely.

## Recommendation
Proceed, revise or stop.

## Approval Required From Faisal
Clearly state what requires approval next.
```

---

# Stop Conditions

Stop and report before continuing if:

- existing architecture conflicts materially with this design;
    
- migration could corrupt current memory stores;
    
- Hermes `state.db` cannot be guaranteed read-only;
    
- a heavyweight dependency becomes unavoidable;
    
- existing tests regress materially;
    
- a destructive change is required;
    
- credentials or secrets are discovered in tracked files;
    
- service installation requires elevated privileges not previously approved.
    

---

# Success Criteria

This milestone is complete only when:

1. Jebat-Cortex runs as a persistent local service.
    
2. Tuah can perform mandatory context preflight through an API.
    
3. Completed work can be recorded through a durable postflight event.
    
4. Queue events survive service restart.
    
5. Jebat and Lekiu can retrieve Cortex memory through MCP.
    
6. Graphify integration remains separate and controlled.
    
7. Context output respects token budgets.
    
8. Memories include provenance and status.
    
9. Hermes `state.db` remains unchanged.
    
10. Tests pass.
    
11. Service can be installed, started, stopped and removed safely.
    
12. No commit, push or deployment occurs without Faisal's approval.
    

Begin with repository assessment and test baseline.

Do not begin by generating speculative new architecture without first inspecting the existing implementation.

Lepas siap build, buat **independent Codex adversarial review** sebelum approve integration dengan Tuah.