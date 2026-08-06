#!/usr/bin/env bash
# Jebat-Cortex v0.2 end-to-end pipeline.
#   1. pick a corpus: the bundled sample log (default) or the real Hermes
#      session DB (--from-hermes, opened READ-ONLY)
#   2. run the Memory Curator over it (Knowledge -> vector, Experience -> graph)
#   3. run the Context Builder on a prompt
#   4. print the merged Markdown digest to stdout
#
# Usage:
#   bash run_cortex.sh ["prompt"]
#   bash run_cortex.sh --from-hermes ["prompt"]
#
# Requires: python3 (3.9+) and bash. ripgrep is optional.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3}"
SAMPLE_LOG="data/sample_conversation.jsonl"
HERMES_DB="${HERMES_DB:-$HOME/.hermes/state.db}"
HERMES_EXPORT="data/hermes_export.jsonl"

# Plain string vars, no arrays: macOS ships bash 3.2, where "${arr[0]:-default}"
# on an empty array still trips `set -u`.
FROM_HERMES=0
PROMPT=""
for arg in "$@"; do
  case "$arg" in
    --from-hermes) FROM_HERMES=1 ;;
    -h|--help)
      echo "usage: bash run_cortex.sh [--from-hermes] [\"prompt\"]"
      exit 0
      ;;
    *)
      if [[ -z "$PROMPT" ]]; then PROMPT="$arg"; fi
      ;;
  esac
done

if [[ -z "$PROMPT" ]]; then
  PROMPT="How does the context builder merge vector and graph memory under the char limits?"
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: $PYTHON not found on PATH" >&2
  exit 1
fi

export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p data

if [[ "$FROM_HERMES" -eq 1 ]]; then
  echo "==> [0/2] Hermes ingest (READ-ONLY)"
  if [[ ! -f "$HERMES_DB" ]]; then
    echo "error: Hermes state DB not found: $HERMES_DB" >&2
    echo "       set HERMES_DB=/path/to/state.db or run a Hermes session first." >&2
    exit 2
  fi
  echo "    source: $HERMES_DB"
  echo "    note:   opened read-only (file:...?mode=ro); the Hermes DB is never written."
  if ! "$PYTHON" -m core.ingest_hermes --db "$HERMES_DB" --out "$HERMES_EXPORT"; then
    echo "error: Hermes ingest failed; aborting before the curator ran." >&2
    exit 2
  fi
  LOG="$HERMES_EXPORT"
  echo
else
  LOG="$SAMPLE_LOG"
fi

if [[ "$FROM_HERMES" -eq 0 && ! -f "$SAMPLE_LOG" ]]; then
  echo "==> generating sample conversation log: $SAMPLE_LOG"
  "$PYTHON" - "$SAMPLE_LOG" <<'PY'
import json, sys

messages = [
    {"role": "user", "ts": "2026-08-06T09:00:00Z",
     "content": "I prefer short, direct answers. Never claim success without test evidence."},
    {"role": "assistant", "ts": "2026-08-06T09:00:30Z",
     "content": "Understood. core/context_builder.py imports core/vector_store.py and core/graph_store.py to merge both memory types."},
    {"role": "user", "ts": "2026-08-06T09:01:00Z",
     "content": "The memory_char_limit is 2200 and the user_char_limit is 1375. Those are hard limits, never exceed them."},
    {"role": "assistant", "ts": "2026-08-06T09:01:40Z",
     "content": "core/memory_curator.py writes to data/vector_store.db for knowledge and data/graph_store.db for experience."},
    {"role": "user", "ts": "2026-08-06T09:02:10Z",
     "content": "My timezone is Asia/Kuala_Lumpur and Faisal is the final approval authority for any deploy."},
    {"role": "assistant", "ts": "2026-08-06T09:02:50Z",
     "content": "tests/test_context_builder.py tests core/context_builder.py budget enforcement."},
    {"role": "user", "ts": "2026-08-06T09:03:20Z",
     "content": "Config rule: retrieval top_k stays at 5 for both stores until we benchmark it."},
    {"role": "assistant", "ts": "2026-08-06T09:04:00Z",
     "content": "The vector store depends on sqlite3 from the standard library; no external vector database is required."},
]

with open(sys.argv[1], "w", encoding="utf-8") as handle:
    for message in messages:
        handle.write(json.dumps(message) + "\n")
print("wrote %d messages" % len(messages))
PY
elif [[ "$FROM_HERMES" -eq 0 ]]; then
  echo "==> using existing sample log: $SAMPLE_LOG"
fi

echo
echo "==> [1/2] Memory Curator"
echo "    corpus: $LOG"
# Reset generated stores so a rebuild never mixes with stale triples from a
# previous run (e.g. an old sample-log edge leaking into a --from-hermes digest).
# These .db files are gitignored, regenerated every run, and never committed.
rm -f "data/vector_store.db" "data/graph_store.db"
"$PYTHON" -m core.memory_curator "$LOG"

echo
echo "==> [2/2] Context Builder"
echo "    prompt: $PROMPT"
echo
"$PYTHON" -m core.context_builder "$PROMPT"

echo "==> pipeline complete"
