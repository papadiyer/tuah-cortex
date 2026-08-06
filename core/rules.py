"""Shared configuration loading, tokenisation and the pluggable embedder.

No network calls and no third-party dependencies in the default path. The
default embedding is a deterministic hashed bag-of-words (plus character
n-grams) vector, so the same text always produces the same vector on any
machine and any Python 3.9+ interpreter.

A stronger semantic backend (sentence-transformers) can be opted into via
``embedding.backend`` in cortex_rules.json. That package is imported lazily, on
instantiation only - importing this module never requires it.
"""

from __future__ import annotations

import abc
import hashlib
import importlib
import importlib.util
import json
import math
import os
import re
import warnings
from typing import Any, Dict, Iterable, List, Optional, Sequence

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


DEFAULT_BACKEND = "deterministic"
SENTENCE_TRANSFORMERS_BACKEND = "sentence-transformers"


class EmbedderUnavailableError(RuntimeError):
    """Raised when a requested embedding backend cannot be used on this host."""


class Embedder(abc.ABC):
    """Interface every embedding backend implements.

    Stores depend on this, not on a concrete function, so a different backend
    can be swapped in without touching the store code.
    """

    name = "embedder"

    @property
    @abc.abstractmethod
    def dimensions(self) -> int:
        """Length of every vector this embedder returns."""

    @property
    def model(self) -> str:
        """Stable identity of the embedding space (backend + model/revision).

        Two embeddings are only comparable when their full identity matches -
        same backend, same model, same dimension. Stores key on this, not just
        on the dimension, so a same-dimension swap (e.g. one sentence-transformer
        model for another) is detected and refused instead of silently mixed.
        """
        return self.name

    @abc.abstractmethod
    def embed(self, text: str) -> List[float]:
        """Return the embedding vector for ``text``."""

    def __call__(self, text: str) -> List[float]:
        return self.embed(text)


class DeterministicEmbedder(Embedder):
    """Hashed bag-of-words + char-ngram vector. Local, stable, dependency-free.

    Word tokens carry full weight; character n-grams carry ``ngram_weight`` so
    that near-miss spellings and shared roots still overlap a little.
    """

    name = DEFAULT_BACKEND

    def __init__(self, rules: Optional[dict] = None):
        self.rules = rules or load_rules()
        cfg = self.rules["embedding"]
        self._dimensions = int(cfg["dimensions"])
        self._ngram_size = int(cfg["ngram_size"])
        self._ngram_weight = float(cfg["ngram_weight"])

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, text: str) -> List[float]:
        dimensions = self._dimensions
        ngram_size = self._ngram_size
        ngram_weight = self._ngram_weight

        vector = [0.0] * dimensions
        for token in tokenize(text):
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


class SentenceTransformerEmbedder(Embedder):
    """Optional semantic backend. Imports sentence-transformers lazily.

    Instantiating this class is the only thing that touches the package. If it
    is not installed, construction raises EmbedderUnavailableError - we never
    pip-install and never silently fall back from here (the caller decides).
    """

    name = SENTENCE_TRANSFORMERS_BACKEND

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", rules: Optional[dict] = None):
        self.rules = rules
        self.model_name = model_name
        try:
            module = importlib.import_module("sentence_transformers")
        except Exception as exc:  # ImportError, or a broken/partial install
            raise EmbedderUnavailableError(
                "sentence-transformers backend unavailable (%s: %s); "
                "install it or set embedding.backend to '%s'"
                % (type(exc).__name__, exc, DEFAULT_BACKEND)
            )
        try:
            self._model = module.SentenceTransformer(model_name)
        except Exception as exc:
            raise EmbedderUnavailableError(
                "could not load sentence-transformers model %r (%s: %s)"
                % (model_name, type(exc).__name__, exc)
            )
        reported = self._model.get_sentence_embedding_dimension()
        if not reported:
            raise EmbedderUnavailableError(
                "model %r reported no embedding dimension" % model_name
            )
        self._dimensions = int(reported)

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model(self) -> str:
        # The concrete model name is the identity of the embedding space; two
        # different sentence-transformer models are NOT interchangeable even
        # when their dimensions match.
        return self.model_name

    def embed(self, text: str) -> List[float]:
        vector = self._model.encode(text or "", normalize_embeddings=True)
        return [float(v) for v in vector]


def sentence_transformers_available() -> bool:
    """True if the optional package can be imported. Never raises."""
    try:
        return importlib.util.find_spec("sentence_transformers") is not None
    except Exception:
        return False


def get_embedder(rules: Optional[dict] = None, **kwargs: Any) -> Embedder:
    """Factory: the backend named in ``embedding.backend``, else deterministic.

    Falls back to the deterministic backend (with a warning, not a crash) when
    sentence-transformers is requested but unusable, so a config change on a
    machine without the package degrades instead of breaking the pipeline.
    """
    rules = rules or load_rules()
    backend = str(rules["embedding"].get("backend", DEFAULT_BACKEND)).strip().lower()

    if backend in (DEFAULT_BACKEND, "", "local", "hashed"):
        return DeterministicEmbedder(rules)

    if backend in (SENTENCE_TRANSFORMERS_BACKEND, "sentence_transformers", "st"):
        model_name = str(
            rules["embedding"].get("model", kwargs.pop("model_name", "all-MiniLM-L6-v2"))
        )
        try:
            return SentenceTransformerEmbedder(model_name, rules=rules)
        except EmbedderUnavailableError as exc:
            warnings.warn(
                "%s - falling back to the %s embedder" % (exc, DEFAULT_BACKEND),
                RuntimeWarning,
                stacklevel=2,
            )
            return DeterministicEmbedder(rules)

    raise ValueError(
        "unknown embedding backend %r (expected %r or %r)"
        % (backend, DEFAULT_BACKEND, SENTENCE_TRANSFORMERS_BACKEND)
    )


def embed(text: str, rules: Optional[dict] = None) -> List[float]:
    """Deterministic L2-normalised embedding (the v0.1 default backend).

    Kept as a module-level function so existing callers and tests keep working;
    it is a thin wrapper over DeterministicEmbedder.
    """
    return DeterministicEmbedder(rules).embed(text)


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
