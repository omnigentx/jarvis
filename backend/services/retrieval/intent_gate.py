"""Multilingual intent gate (spec §7, English-first §7).

Embeds the user message with BAAI/bge-m3 and matches it by cosine similarity to
the English prototype sentences in helpers/memory_triggers.PROTOTYPE_INTENTS.
Because BGE-M3 maps all languages into one space, this detects "recall past /
state a preference / ..." intents in ANY language while only English text lives
in the code. Verified cross-lingual (VN/FR/JA/ES/DE intents score ~0.73-0.96 vs
~0.49-0.61 for chitchat).

Falls back to the English substring lexicon when embeddings are unavailable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from helpers.memory_triggers import (
    CAPTURE_PROTOTYPES,
    CAPTURE_TARGETS,
    RECALL_PROTOTYPES,
    classify_targets,
)
from services.indexing.embedding_provider import EmbeddingProvider

DEFAULT_THRESHOLD = 0.67


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class IntentResult:
    targets: set[str] = field(default_factory=set)   # recall targets
    capture: bool = False
    capture_type: str | None = None                  # "pinned" | "semantic"
    scores: dict[str, float] = field(default_factory=dict)
    via: str = "embedding"                            # "embedding" | "lexicon"


class IntentGate:
    def __init__(self, embedding_provider: EmbeddingProvider,
                 threshold: float = DEFAULT_THRESHOLD):
        self._emb = embedding_provider
        self._threshold = threshold
        self._recall_vecs: dict[str, list[list[float]]] | None = None
        self._capture_vecs: dict[str, list[list[float]]] | None = None

    def _ensure_prototypes(self) -> None:
        if self._recall_vecs is None:
            self._recall_vecs = {t: self._emb.embed_documents(s)
                                 for t, s in RECALL_PROTOTYPES.items()}
            self._capture_vecs = {t: self._emb.embed_documents(s)
                                  for t, s in CAPTURE_PROTOTYPES.items()}

    def classify(self, text: str, *, lexicon_overrides: dict | None = None) -> IntentResult:
        """Recall targets (user ASKING about past) + capture flag/type (user
        STATING something durable). Embedding when available; else the English
        substring lexicon."""
        text = (text or "").strip()
        if not text:
            return IntentResult(via="embedding")
        if not self._emb.is_available():
            targets = classify_targets(text, overrides=lexicon_overrides)
            cap = bool(targets & CAPTURE_TARGETS)
            return IntentResult(targets=targets, capture=cap,
                                capture_type="pinned" if cap else None, via="lexicon")

        self._ensure_prototypes()
        v = self._emb.embed_query(text)
        scores: dict[str, float] = {}

        recall: set[str] = set()
        for target, vecs in self._recall_vecs.items():
            sim = max(_cosine(v, pv) for pv in vecs)
            scores[f"recall:{target}"] = sim
            if sim >= self._threshold:
                recall.add(target)

        best_cap, best_cap_sim = None, 0.0
        for cap_type, vecs in self._capture_vecs.items():
            sim = max(_cosine(v, pv) for pv in vecs)
            scores[f"capture:{cap_type}"] = sim
            if sim > best_cap_sim:
                best_cap, best_cap_sim = cap_type, sim

        # A QUESTION about a stored fact ("what's my job?") embeds close to the
        # fact-statement prototype ("I work as a software engineer"), so a naive
        # ``cap >= threshold`` false-fires and stores the question as a memory.
        # Only capture when the STATE intent strictly beats any RECALL intent,
        # AND the text isn't interrogative. "?" is a near-universal cross-lingual
        # question marker, so this stays language-agnostic (English-first §7).
        best_recall_sim = max((scores[f"recall:{t}"] for t in self._recall_vecs),
                              default=0.0)
        is_question = text.endswith("?")
        capture = (best_cap_sim >= self._threshold
                   and best_cap_sim > best_recall_sim
                   and not is_question)

        return IntentResult(targets=recall, capture=capture,
                            capture_type=best_cap if capture else None,
                            scores=scores, via="embedding")
