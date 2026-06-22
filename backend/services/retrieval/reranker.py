"""Cross-encoder reranker — the memory-v2 precision stage (spec §relevance).

The dense lane is a BI-ENCODER: it embeds the query and each memory SEPARATELY,
so it can't judge fine relevance — "how old is my baby" scores "I bought a cat"
almost as high as "I have a 7-month-old son" (measured 2026-06-22). A CROSS-encoder
reads (query, memory) TOGETHER and scores their relevance directly. That's the
standard RAG fix; it's too slow to run over the whole store, so it runs only over
the small FUSED candidate set at query time, re-orders it, and drops candidates
below a score floor.

Main-process-only + lazy + Null fallback, mirroring ``embedding_provider`` — an
agent subprocess must never load a model; a missing dep degrades to "no rerank"
(fusion order kept) rather than crashing.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger("memory.reranker")

DEFAULT_RERANKER = "BAAI/bge-reranker-v2-m3"
# Set by the FastAPI lifespan in the main process (same flag the embedder uses).
MAIN_PROCESS_ENV = "JARVIS_MAIN_PROCESS"


class Reranker(ABC):
    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Relevance score per document (higher = more relevant), SAME order as
        ``documents``."""


class NullReranker(Reranker):
    """Used when the reranker dep/model is unavailable — callers check
    ``is_available()`` and keep the fusion order."""

    def __init__(self, reason: str):
        self.reason = reason

    def is_available(self) -> bool:
        return False

    def rerank(self, query, documents):
        raise RuntimeError(f"reranker unavailable: {self.reason}")


class CrossEncoderReranker(Reranker):
    """Lazy ``sentence-transformers`` CrossEncoder (e.g. ``bge-reranker-v2-m3``),
    loaded once in the main process. (FlagEmbedding's FlagReranker is NOT used —
    it calls ``tokenizer.prepare_for_model`` which transformers 5.x removed.)"""

    def __init__(self, model_name: str = DEFAULT_RERANKER, max_length: int = 512):
        self._model_name = model_name
        self._max_length = max_length
        self._model = None

    def is_available(self) -> bool:
        return True

    def _ensure_model(self):
        if self._model is not None:
            return
        if not os.environ.get(MAIN_PROCESS_ENV):
            raise RuntimeError(
                "reranker load attempted outside the main backend process; "
                "agents must use the memory API, not load models.")
        from sentence_transformers import CrossEncoder  # heavy, lazy
        self._model = CrossEncoder(self._model_name, max_length=self._max_length)
        logger.info("[MEMORY] Loaded reranker %s", self._model_name)

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        self._ensure_model()
        scores = self._model.predict([[query, d] for d in documents])
        return [float(s) for s in scores]


def _have_st() -> bool:
    import importlib.util
    return importlib.util.find_spec("sentence_transformers") is not None


_WARNED = False


def get_reranker(model_name: str = DEFAULT_RERANKER) -> Reranker:
    if not _have_st():
        global _WARNED
        if not _WARNED:
            _WARNED = True
            logger.warning("[MEMORY] sentence-transformers not installed — reranker "
                           "disabled; recall keeps fusion order. Install the 'memory' extra.")
        return NullReranker("sentence-transformers not installed")
    return CrossEncoderReranker(model_name)


# Process-wide shared instance (the model is loaded once; the orchestrator reuses it).
_SHARED: Reranker | None = None
_SHARED_KEY: str | None = None


def get_shared_reranker(model_name: str = DEFAULT_RERANKER) -> Reranker:
    global _SHARED, _SHARED_KEY
    if _SHARED is None or _SHARED_KEY != model_name:
        _SHARED = get_reranker(model_name)
        _SHARED_KEY = model_name
    return _SHARED
