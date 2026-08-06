"""Shared configuration loading, tokenisation and the local embedding function.

No network calls, no third-party dependencies. The embedding is a deterministic
hashed bag-of-words (plus character n-grams) vector, so the same text always
produces the same vector on any machine and any Python 3.9+ interpreter.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from typing import Dict, Iterable, List, Optional, Sequence

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RULES_PATH = os.path.join(REPO_ROOT, "config", "cortex_rules.json")

_TOKEN_RE = re.compile(r"[a-z0-9_./-]+")

# Very small stop list: enough to stop function words dominating similarity,
# small enough that we never drop a domain term.
_STOPWORDS = frozenset(
    """a an and are as at be but by for from has have he her his i if in into is it
    its me my of on or our she that the their them then there these they this to us
    was we were what when which who will with you your""".split()
)

_rules_cache: Dict[str, dict] = {}


def load_rules(path: Optional[str] = None, use_cache: bool = True) -> dict:
    """Load cortex_rules.json. Raises if missing or malformed - never guesses."""
    resolved = os.path.abspath(path or DEFAULT_RULES_PATH)
    if use_cache and resolved in _rules_cache:
        return _rules_cache[resolved]
    if not os.path.exists(resolved):
        raise FileNotFoundError("cortex rules not found: %s" % resolved)
    with open(resolved, "r", encoding="utf-8") as handle:
        rules = json.load(handle)
    for section in ("limits", "retrieval", "classification", "embedding", "paths"):
        if section not in rules:
            raise ValueError("cortex rules missing required section: %s" % section)
    if use_cache:
        _rules_cache[resolved] = rules
    return rules


def repo_path(*parts: str) -> str:
    """Resolve a path relative to the repository root."""
    return os.path.join(REPO_ROOT, *parts)


def tokenize(text: str) -> List[str]:
    """Lowercase word tokens with stopwords removed. Keeps paths/underscores."""
    tokens = _TOKEN_RE.findall((text or "").lower())
    return [t for t in tokens if t not in _STOPWORDS and not t.isdigit()]


def _bucket(token: str, dimensions: int) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % dimensions


def embed(text: str, rules: Optional[dict] = None) -> List[float]:
    """Deterministic L2-normalised hashed bag-of-words + char-ngram vector.

    Word tokens carry full weight; character n-grams carry ``ngram_weight`` so
    that near-miss spellings and shared roots still overlap a little.
    """
    rules = rules or load_rules()
    cfg = rules["embedding"]
    dimensions = int(cfg["dimensions"])
    ngram_size = int(cfg["ngram_size"])
    ngram_weight = float(cfg["ngram_weight"])

    vector = [0.0] * dimensions
    tokens = tokenize(text)
    for token in tokens:
        vector[_bucket(token, dimensions)] += 1.0
        if ngram_weight > 0 and len(token) > ngram_size:
            for i in range(len(token) - ngram_size + 1):
                gram = token[i : i + ngram_size]
                vector[_bucket("#" + gram, dimensions)] += ngram_weight

    # Sub-linear term damping, then L2 normalisation so cosine == dot product.
    for i, value in enumerate(vector):
        if value:
            vector[i] = 1.0 + math.log(value)
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity. Returns 0.0 for zero vectors or length mismatch."""
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def keyword_overlap(text: str, keywords: Iterable[str]) -> int:
    """Count how many keywords/phrases appear in text (word-boundary aware)."""
    lowered = (text or "").lower()
    tokens = set(_TOKEN_RE.findall(lowered))
    hits = 0
    for keyword in keywords:
        key = keyword.lower()
        if " " in key:
            if key in lowered:
                hits += 1
        elif key in tokens:
            hits += 1
    return hits


def truncate(text: str, limit: int, marker: str = " ...[truncated]") -> str:
    """Hard-truncate to ``limit`` characters, preferring a word boundary.

    The returned string is guaranteed to be <= limit characters.
    """
    if limit <= 0:
        return ""
    text = text or ""
    if len(text) <= limit:
        return text
    if limit <= len(marker):
        return text[:limit]
    budget = limit - len(marker)
    cut = text[:budget]
    space = cut.rfind(" ")
    if space > budget * 0.6:
        cut = cut[:space]
    return (cut + marker)[:limit]
