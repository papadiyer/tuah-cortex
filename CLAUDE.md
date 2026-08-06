# Jebat-Cortex

Automated Memory Pipeline modelling the prefrontal cortex: long-term memory,
context indexing, and information management for Jebat/Hermes.

## Architecture

```
Chat -> Conversation -> Memory Curator -> (split: Experience & Knowledge)
  |- Experience  -> Graph Store  (relational / code graph; ripgrep fallback)
  |- Knowledge   -> Vector Store (semantic long-term memory)
Both merge at Context Builder -> injected into Jebat system prompt.
```

## Roles

- Jebat/Hermes = Architect & Memory Curator (orchestrates, validates).
- Lekiu (Claude Code) = Builder (writes the codebase).
- Faisal = final approval authority.

## Constraints

- `user_char_limit` = 1375, `memory_char_limit` = 2200 (see config/cortex_rules.json).
- No secrets, no credentials, no `.env` edits.
- No commit/push/merge/deploy unless Faisal approves.
- Smallest correct change; tests are proof; report plainly.

## Repo layout

```
core/        memory_curator.py, vector_store.py, graph_store.py, context_builder.py
config/      cortex_rules.json
tests/       unit + fixture based
data/        sample conversation logs + generated stores
run_cortex.sh
```
