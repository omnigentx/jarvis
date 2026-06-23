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
# model/revision change forces a new vector index rather than silently
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


# Query-side instruction for instruction-aware encoders (Qwen3-Embedding et al.).
# Documents are embedded plain; only the query gets this prefix — the asymmetric
# question→fact framing the model was trained on. Calibrated on real recall data
# 2026-06-22 (Qwen3-Embedding-0.6B: on-topic ≥0.34, off-topic ≤0.33).
ST_QUERY_INSTRUCTION = ("Given a user's question, retrieve the user's personal "
                        "memory facts relevant to answering it")


class SentenceTransformerEmbeddingProvider(EmbeddingProvider):
    """Dense embeddings via ``sentence-transformers`` (Qwen3-Embedding, etc.).
    Lazy + main-process-only, like the BGE provider. Instruction-aware: the
    QUERY gets the task-instruction prefix (the model's convention); documents
    are embedded plain. Output is L2-normalized so the LadybugDB cosine metric
    and the ``recall_min_similarity`` gate behave as configured."""

    def __init__(self, model_name: str, revision: str = "",
                 query_instruction: str = ST_QUERY_INSTRUCTION, dim: int = BGE_M3_DIM):
        self._model_name = model_name
        self._revision = revision
        self._instruct = query_instruction
        self._dim = dim
        self._model = None

    def is_available(self) -> bool:
        return True

    def revision(self) -> str:
        return self._revision or self._model_name

    def dim(self) -> int:
        return self._dim

    def _ensure_model(self):
        if self._model is not None:
            return
        if not os.environ.get(MAIN_PROCESS_ENV):
            raise RuntimeError(
                "embedding model load attempted outside the main backend process; "
                "agents must use the memory API, not load embedding models.")
        from sentence_transformers import SentenceTransformer  # heavy, lazy
        kwargs = {"revision": self._revision} if self._revision else {}
        self._model = SentenceTransformer(self._model_name, **kwargs)
        # get_embedding_dimension is the current name; fall back for older ST.
        _dim_fn = (getattr(self._model, "get_embedding_dimension", None)
                   or self._model.get_sentence_embedding_dimension)
        d = _dim_fn()
        if d:
            self._dim = int(d)
        logger.info("[MEMORY] Loaded embedding model %s (sentence-transformers, dim=%d)",
                    self.revision(), self._dim)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self._ensure_model()
        vecs = self._model.encode(list(texts), normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]

    def embed_query(self, text: str) -> list[float]:
        self._ensure_model()
        q = f"Instruct: {self._instruct}\nQuery:{text}" if self._instruct else text
        v = self._model.encode([q], normalize_embeddings=True)[0]
        return list(map(float, v))


def _is_bge_m3(model_name: str) -> bool:
    return "bge-m3" in (model_name or "").lower()


def _dep_for(model_name: str) -> str:
    """Which optional package a model needs: bge-m3 → FlagEmbedding; everything
    else (Qwen3-Embedding, …) → sentence-transformers."""
    return "FlagEmbedding" if _is_bge_m3(model_name) else "sentence_transformers"


def _have(pkg: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(pkg) is not None


_WARNED_MISSING_DEPS = False


def get_embedding_provider(model_name: str = DEFAULT_MODEL,
                           revision: str = "") -> EmbeddingProvider:
    """Return a usable provider, or a Null provider (degraded) if the model's
    backend package is not installed. Dispatches by model name: ``bge-m3`` →
    FlagEmbedding; anything else → sentence-transformers (Qwen3-Embedding, …).

    FTS-only is a SUPPORTED mode, so a missing dep does not crash — but it must
    NOT be silent (the prod incident where the graph stayed empty for an hour):
    log it LOUD, once."""
    dep = _dep_for(model_name)
    if not _have(dep):
        global _WARNED_MISSING_DEPS
        if not _WARNED_MISSING_DEPS:
            _WARNED_MISSING_DEPS = True
            logger.error(
                "[MEMORY] %s is NOT installed — dense vector recall AND the "
                "knowledge graph are DISABLED for model %r; memory degraded to "
                "FTS-only. Index writes DEFER (graph stays empty). Fix: install "
                "the 'memory' extra.", dep, model_name)
        return NullEmbeddingProvider(f"{dep} not installed")
    if _is_bge_m3(model_name):
        return BGEEmbeddingProvider(model_name, revision)
    return SentenceTransformerEmbeddingProvider(model_name, revision)


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
