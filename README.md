# 🧠 Tuah-Cortex

> **Local, OpenAI-free embeddings for your OpenClaw agent's long-term memory.**
> Drop the `429 insufficient_quota`. Keep your agent's brain on your own machine.

Tuah-Cortex is a **standalone fork** of Jebat-Cortex — a prefrontal-cortex-style
memory pipeline (vector + graph + context builder) — extended with one sharp
addition: an **OpenAI-compatible `/v1/embeddings` endpoint** backed by a local
`sentence-transformers` model (MiniLM, 384-dim).

Point OpenClaw's `memory-lancedb` plugin at it and your agent's RAG/memory
embeddings run **100% locally**. No API key. No quota. No bill.

```
┌─────────────┐     embeddings      ┌──────────────────┐
│  OpenClaw   │ ──── POST /v1 ───▶ │   Tuah-Cortex    │
│ memory-     │     embeddings      │   :8766 (local)  │
│ lancedb     │ ◀── 384-dim vec ─── │  MiniLM in-proc  │
└─────────────┘                    └──────────────────┘
        ▲                                      │
        │ 0 OpenAI calls                       │ 0 cost
        └──────────────────────────────────────┘
```

---

## ✨ Why

OpenClaw's `memory-lancedb` plugin needs an embeddings backend. Out of the box it
points at OpenAI → you hit `429` the moment your free quota dies and your agent
goes amnesiac mid-conversation.

Tuah-Cortex **is** the backend. Same OpenAI-compatible request/response shape,
served from a model that's already loaded in-process. One config block and the
429 is gone for good.

**Verified in production:** a live Telegram group message → Tuah auto-captures →
embeds via Tuah-Cortex → vector stored in LanceDB (`vecDim: 384`) → **0 OpenAI
429**.

---

## 🚀 Quick start

```bash
# 1. Clone
git clone https://github.com/papadiyer/tuah-cortex.git
cd tuah-cortex

# 2. Create venv + install (CPU torch + sentence-transformers)
python3.11 -m venv .venv-cortex-st
. .venv-cortex-st/bin/activate
pip install "sentence-transformers==5.7.0" torch

# 3. Run the API (binds 127.0.0.1:8766 only)
python -m cli.cortex_cli serve --host 127.0.0.1 --port 8766

# 4. Prove it works
curl -s http://127.0.0.1:8766/v1/health
curl -s -X POST http://127.0.0.1:8766/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"x","input":"tuah ingat semua"}'
# → {"data":[{"embedding":[384 floats],"index":0}], ...}
```

### 🍏 macOS launchd (reboot-safe)

```bash
cp service/launchd/com.m5.tuah-cortex.plist ~/Library/LaunchAgents/
cp service/launchd/com.m5.tuah-cortex-worker.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.m5.tuah-cortex.plist
launchctl load ~/Library/LaunchAgents/com.m5.tuah-cortex-worker.plist
curl -s http://127.0.0.1:8766/v1/health   # → ready
```

> The worker drains the ingestion queue. Load **both** plists or events pile up
> and nothing ever gets embedded.

---

## 🔌 Wire it to OpenClaw (memory-lancedb)

In your OpenClaw config (`m5/config/openclaw.m5.json`):

```jsonc
{
  "plugins": {
    "enabled": true,
    "entries": {
      "memory-lancedb": {
        "enabled": true,                       // BOTH flags required
        "config": {
          "embedding": {
            "provider": "openai",               // OpenAI-COMPATIBLE shape, NOT api.openai.com
            "model": "sentence-transformers-paraphrase-multilingual-MiniLM-L12-v2",
            "baseUrl": "http://127.0.0.1:8766/v1",
            "apiKey": "***",                    // placeholder, not validated
            "dimensions": 384
          },
          "autoCapture": true,
          "autoRecall": true,
          "customTriggers": ["tuah", "jebat", "cortex", "m5", "ingat"]
        }
      }
    },
    "slots": { "memory": "memory-lancedb" }     // replaces builtin memory-core
  }
}
```

- `plugins.enabled` **and** `entries.memory-lancedb.enabled` must both be `true`
  or the engine shows as "disabled" in the UI and memory never runs.
- `slots.memory = "memory-lancedb"` swaps out the builtin `memory-core` (the thing
  that was 429-ing on OpenAI).
- `customTriggers` makes group chat actually capture. Casual messages without a
  trigger word are skipped by design (keeps noise out of memory).

Restart the gateway. Done — your agent now remembers locally.

---

## 🏗️ Architecture

```
Chat → Conversation → Memory Curator → (split)
  ├─ Experience  → Graph Store   (relational / code graph; ripgrep fallback)
  └─ Knowledge   → Vector Store  (semantic long-term memory, 384-dim MiniLM)
Both merge at Context Builder → injected into the agent's system prompt.
```

Plus the Tuah fork addition:

```
api/embeddings.py  →  POST /v1/embeddings
   reuses the in-process SentenceTransformerEmbedder (no extra model load)
   OpenAI-compatible request/response shape
```

| Layer | Tech |
|---|---|
| Embeddings | `sentence-transformers` paraphrase-multilingual-MiniLM-L12-v2 (384-dim) |
| Vector store | LanceDB (via OpenClaw `memory-lancedb`) |
| API | stdlib `http.server`, localhost-only, no external deps |
| Memory pipeline | vector + graph + context builder (inherited from Jebat-Cortex) |

---

## 📁 Layout

```
api/          OpenAI-compatible HTTP surface (/v1/embeddings, /v1/health, ...)
core/         memory_curator, vector_store, graph_store, context_builder, rules
config/       cortex_rules.json, identity.json
service/      launchd plists (macOS)
tests/        unit + fixture based
run_cortex.sh entrypoint
data/         sample logs + generated stores (gitignored)
```

---

## ✅ Status

- [x] `/v1/embeddings` returns 384-dim vectors, OpenAI shape
- [x] Wired to OpenClaw `memory-lancedb` → live memory persisted (LanceDB)
- [x] **0 OpenAI 429** in production gateway log
- [x] launchd plists for reboot-safe localhost serving
- [x] Test suite green (`python3 -m unittest discover -s tests`)

---

## 📜 License

[MIT](LICENSE) — do whatever, just keep the copyright notice.

---

<p align="center">
  <sub>Built for Tuah · runs on your laptop · no cloud required</sub>
</p>
