#!/usr/bin/env bash
# Jebat-Cortex v0.1 end-to-end pipeline.
#   1. ensure a sample conversation log exists
#   2. run the Memory Curator over it (Knowledge -> vector, Experience -> graph)
#   3. run the Context Builder on a sample prompt
#   4. print the merged Markdown digest to stdout
#
# Requires: python3 (3.9+) and bash. ripgrep is optional.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3}"
SAMPLE_LOG="data/sample_conversation.jsonl"
PROMPT="${1:-How does the context builder merge vector and graph memory under the char limits?}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: $PYTHON not found on PATH" >&2
  exit 1
fi

export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p data

if [[ ! -f "$SAMPLE_LOG" ]]; then
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
else
  echo "==> using existing sample log: $SAMPLE_LOG"
fi

echo
echo "==> [1/2] Memory Curator"
"$PYTHON" -m core.memory_curator "$SAMPLE_LOG"

echo
echo "==> [2/2] Context Builder"
echo "    prompt: $PROMPT"
echo
"$PYTHON" -m core.context_builder "$PROMPT"

echo "==> pipeline complete"
