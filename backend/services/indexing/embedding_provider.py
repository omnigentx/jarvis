"""Embedding provider interface + lazy BGE-M3 implementation + Null fallback.

The model loads ONLY in the main backend process (never inside a spawned
agent subprocess — agents reach memory via the MCP tool/API, never by loading
a model). When the embedding deps or model are unavailable the factory
returns ``NullEmbeddingProvider`` so the system degrades to BM25/FTS5 instead
of crashing (spec §8.3, §20).
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger("memory.embedding")

# Pinned default (overridable via settings). Revision is pinned separately so a
# model/revision change forces a new Qdrant collection rather than silently
# mixing vector spaces.
DEFAULT_MODEL = "BAAI/bge-m3"
BGE_M3_DIM = 1024

# The main backend process sets this in the FastAPI lifespan. The BGE provider
# refuses to load a model when it is absent, so an accidental import inside a
# spawned agent subprocess fails loudly instead of loading a second copy.
MAIN_PROCESS_ENV = "JARVIS_MAIN_PROCESS"


class EmbeddingProvider(ABC):
    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def revision(self) -> str: ...

    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    def embed_query(self, text: str) -> list[float]: ...


class NullEmbeddingProvider(EmbeddingProvider):
    """Used when embeddings are unavailable. Reports unavailable and raises if
    anyone tries to embed (callers must check ``is_available()`` and fall back
    to BM25/FTS5)."""

    def __init__(self, reason: str):
        self.reason = reason

    def is_available(self) -> bool:
        return False

    def revision(self) -> str:
        return ""

    def dim(self) -> int:
        return BGE_M3_DIM

    def _fail(self):
        raise RuntimeError(f"embeddings unavailable: {self.reason}")

    def embed_documents(self, texts):
        self._fail()

    def embed_query(self, text):
        self._fail()


class BGEEmbeddingProvider(EmbeddingProvider):
    """Lazy BGE-M3 dense embeddings via FlagEmbedding. The model is loaded on
    first use (a singleton), in the main process only."""

    def __init__(self, model_name: str = DEFAULT_MODEL, revision: str = ""):
        self._model_name = model_name
        self._revision = revision
        self._model = None

    def is_available(self) -> bool:
        return True

    def revision(self) -> str:
        return self._revision or self._model_name

    def dim(self) -> int:
        return BGE_M3_DIM

    def _ensure_model(self):
        if self._model is not None:
            return
        if not os.environ.get(MAIN_PROCESS_ENV):
            raise RuntimeError(
                "BGE model load attempted outside the main backend process; "
                "agents must use the memory API, not load embedding models."
            )
        from FlagEmbedding import BGEM3FlagModel  # heavy, lazy
        kwargs = {"use_fp16": True}
        if self._revision:
            kwargs["revision"] = self._revision
        self._model = BGEM3FlagModel(self._model_name, **kwargs)
        logger.info("[MEMORY] Loaded embedding model %s", self.revision())

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self._ensure_model()
        out = self._model.encode(texts, return_dense=True, return_sparse=False)
        return [list(map(float, v)) for v in out["dense_vecs"]]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def _deps_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("FlagEmbedding") is not None


_WARNED_MISSING_DEPS = False


def get_embedding_provider(model_name: str = DEFAULT_MODEL,
                           revision: str = "") -> EmbeddingProvider:
    """Return a usable provider, or a Null provider (degraded) if the embedding
    deps are not installed.

    FTS-only is a SUPPORTED mode (the ``memory`` extra is optional), so a missing
    dep does not crash — but it must NOT be silent: a memory-enabled deployment
    expecting dense recall + the knowledge graph would otherwise degrade with no
    signal (the prod incident where the graph stayed empty for an hour). Log it
    LOUD, once."""
    if not _deps_available():
        global _WARNED_MISSING_DEPS
        if not _WARNED_MISSING_DEPS:
            _WARNED_MISSING_DEPS = True
            logger.error(
                "[MEMORY] FlagEmbedding is NOT installed — dense vector recall AND "
                "the knowledge graph are DISABLED; memory degraded to FTS-only "
                "keyword search. Index writes will DEFER (graph stays empty). Fix: "
                "install the 'memory' extra (uv sync --extra memory / "
                "pip install 'FlagEmbedding>=1.4.0').")
        return NullEmbeddingProvider("FlagEmbedding not installed")
    return BGEEmbeddingProvider(model_name, revision)


# Process-wide shared instance. The BGE model is ~2.3 GB and ~seconds to load
# from disk — creating a fresh provider per request would reload it every time.
# The worker, the retrieval orchestrator, and the auto-inject hook all share
# this ONE instance so the model loads exactly once.
_SHARED: EmbeddingProvider | None = None
_SHARED_KEY: tuple[str, str] | None = None


def get_shared_embedding_provider(model_name: str = DEFAULT_MODEL,
                                  revision: str = "") -> EmbeddingProvider:
    global _SHARED, _SHARED_KEY
    key = (model_name, revision)
    if _SHARED is None or _SHARED_KEY != key:
        _SHARED = get_embedding_provider(model_name, revision)
        _SHARED_KEY = key
    return _SHARED
