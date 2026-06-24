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

DEFAULT_RERANKER = "Qwen/Qwen3-Reranker-0.6B"
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


# Query-side instruction for Qwen3-Reranker — the personal-relevance framing.
# Measured 2026-06-23 head-to-head on the live Vietnamese store: Qwen3-Reranker
# ranks the direct answer #1 ("where do I work" → Techcombank 0.245 live) where
# bge-reranker-v2-m3 buried it at 0.0013, and gives a usable score SPREAD (so a
# floor can gate) — bge compressed every score near 0, ungateable. The cost is
# latency: one batched forward pass of a 0.6B LM over the candidates (~1.2–1.4s
# p50/p95 end-to-end on CPU, vs bge's classification head) — accelerated to
# cuda/mps when present. Worth it for the ranking + precision gain.
QWEN_RERANK_INSTRUCT = (
    "The user is searching their OWN personal memory. A document is relevant ONLY "
    "if it states a personal fact about the user needed to answer this specific "
    "question about themselves; a general-knowledge question that merely mentions "
    "a keyword is NOT relevant.")


class Qwen3Reranker(Reranker):
    """Qwen3-Reranker scored via the yes/no NEXT-TOKEN logit — a causal LM, NOT a
    SequenceClassification cross-encoder, so it can't use the CrossEncoder path.
    Instruction-aware. Lazy + main-process-only, like the embedder."""

    def __init__(self, model_name: str, max_length: int = 512,
                 instruct: str = QWEN_RERANK_INSTRUCT):
        self._model_name = model_name
        self._max_length = max_length
        self._instruct = instruct
        self._model = None
        self._tok = None
        self._yes = self._no = None
        self._pre = self._suf = None
        self._device = "cpu"

    def is_available(self) -> bool:
        return True

    def _ensure_model(self):
        if self._model is not None:
            return
        if not os.environ.get(MAIN_PROCESS_ENV):
            raise RuntimeError(
                "reranker load attempted outside the main backend process; "
                "agents must use the memory API, not load models.")
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer  # heavy, lazy
        # Use an accelerator when present (the 0.6B forward dominates recall
        # latency on CPU); fp16 there, fp32 on CPU for numerical safety.
        self._device = ("cuda" if torch.cuda.is_available()
                        else "mps" if torch.backends.mps.is_available() else "cpu")
        dtype = torch.float16 if self._device != "cpu" else torch.float32
        self._tok = AutoTokenizer.from_pretrained(self._model_name, padding_side="left")
        self._model = (AutoModelForCausalLM.from_pretrained(self._model_name, dtype=dtype)
                       .to(self._device).eval())
        self._yes = self._tok.convert_tokens_to_ids("yes")
        self._no = self._tok.convert_tokens_to_ids("no")
        # The fixed system/assistant scaffold around each (instruct, query, doc).
        self._pre = self._tok.encode(
            "<|im_start|>system\nJudge whether the Document meets the requirements "
            "based on the Query and the Instruct provided. Note that the answer can "
            "only be \"yes\" or \"no\".<|im_end|>\n<|im_start|>user\n",
            add_special_tokens=False)
        self._suf = self._tok.encode(
            "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n",
            add_special_tokens=False)
        logger.info("[MEMORY] Loaded reranker %s (Qwen3 yes/no logit, device=%s)",
                    self._model_name, self._device)

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        self._ensure_model()
        import torch
        seqs = [
            self._pre + self._tok.encode(
                f"<Instruct>: {self._instruct}\n<Query>: {query}\n<Document>: {d}",
                add_special_tokens=False, max_length=self._max_length, truncation=True)
            + self._suf
            for d in documents
        ]
        ml = max(len(s) for s in seqs)
        pad = self._tok.pad_token_id or 0
        dev = self._device
        ids = torch.tensor([[pad] * (ml - len(s)) + s for s in seqs], device=dev)   # left-pad
        att = torch.tensor([[0] * (ml - len(s)) + [1] * len(s) for s in seqs], device=dev)
        with torch.no_grad():
            logits = self._model(input_ids=ids, attention_mask=att).logits[:, -1, :].float()
        # P(yes) from the softmax over the [no, yes] next-token logits.
        probs = torch.softmax(torch.stack([logits[:, self._no], logits[:, self._yes]], 1), 1)[:, 1]
        return [float(p) for p in probs]


def _is_qwen(model_name: str) -> bool:
    return "qwen" in (model_name or "").lower()


def _have_st() -> bool:
    import importlib.util
    return importlib.util.find_spec("sentence_transformers") is not None


def _have_transformers() -> bool:
    import importlib.util
    return importlib.util.find_spec("transformers") is not None


_WARNED = False


def _warn_once(reason: str) -> None:
    global _WARNED
    if not _WARNED:
        _WARNED = True
        logger.warning("[MEMORY] %s not installed — reranker disabled; recall keeps "
                       "fusion order. Install the 'memory' extra.", reason)


def get_reranker(model_name: str = DEFAULT_RERANKER) -> Reranker:
    # Dispatch by model, each behind ITS OWN availability gate: Qwen3-Reranker is a
    # causal LM that needs `transformers` (NOT sentence-transformers); bge & other
    # cross-encoders use the sentence-transformers CrossEncoder path. Gating the
    # whole function on `_have_st()` wrongly disabled Qwen whenever ST was missing.
    if _is_qwen(model_name):
        if not _have_transformers():
            _warn_once("transformers")
            return NullReranker("transformers not installed")
        return Qwen3Reranker(model_name)
    if not _have_st():
        _warn_once("sentence-transformers")
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
