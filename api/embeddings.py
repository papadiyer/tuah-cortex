"""POST /v1/embeddings — OpenAI-compatible embeddings endpoint.

Tuah-Cortex fork addition: bridges OpenClaw's `memory-lancedb` plugin (which
calls an OpenAI-style `/embeddings` endpoint) to Tuah-Cortex's local
sentence-transformers embedder. This lets Tuah keep long-term memory without
calling OpenAI (no 429, no cost) — the embedder is already loaded in-process.

Request (OpenAI shape):
    {"model": "<any>", "input": "string" | ["string", ...]}
Response:
    {"data": [{"object": "embedding", "index": 0, "embedding": [...]}, ...],
     "model": "<model>", "usage": {"prompt_tokens": N, "total_tokens": N}}
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from api.service import CortexService
from core.rules import get_embedder

Result = Tuple[int, Dict[str, Any]]


def _embed_one(service: CortexService, text: str) -> list[float]:
    # get_embedder is cheap (cached model load in SentenceTransformerEmbedder);
    # reuse the service's rules so backend/model config stays single-sourced.
    embedder = get_embedder(service.rules)
    return embedder.embed(text or "")


def post_embeddings(service: CortexService, body: Any) -> Result:
    # Definitive access trace (greppable file) so we can prove Tuah hits 8766.
    try:
        with open("/tmp/tuah-cortex-embeddings.log", "a", encoding="utf-8") as _f:
            _f.write("%s hit from tuah gateway\n" % __import__("datetime").datetime.now().isoformat())
    except Exception:
        pass
    if not isinstance(body, dict):
        return 400, {"error": {"message": "request body must be a JSON object", "type": "invalid_request"}}
    model = body.get("model", "sentence-transformers-paraphrase-multilingual-MiniLM-L12-v2")
    inp = body.get("input", "")
    if isinstance(inp, str):
        inputs = [inp]
    elif isinstance(inp, list):
        inputs = [str(x) for x in inp]
    else:
        return 400, {"error": {"message": "input must be a string or list of strings", "type": "invalid_request"}}

    data = []
    for idx, text in enumerate(inputs):
        vec = _embed_one(service, text)
        data.append({"object": "embedding", "index": idx, "embedding": vec})

    # Lightweight token estimate: ~4 chars/token, floor of 1.
    prompt_tokens = max(1, sum(len(t) // 4 for t in inputs))
    return 200, {
        "object": "list",
        "data": data,
        "model": model,
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
    }


ROUTES = (("POST", "/v1/embeddings", "post_embeddings"),)
