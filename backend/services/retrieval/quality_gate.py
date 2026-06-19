"""Deterministic quality gate — decides Level 1 → Level 2 escalation with
numeric thresholds only (NO LLM, no scattered conditionals). Thresholds are
data-driven (settings.quality_gate_thresholds overrides the defaults).
"""
from __future__ import annotations

from services.retrieval.contracts import Evidence

DEFAULT_THRESHOLDS = {
    "min_results": 1,            # fewer than this → weak
    "min_top_score": 0.015,      # top fused score below this → weak
    "max_rank_disagreement": 8,  # BM25 vs dense rank gap on the top item → weak
}


def _merged(thresholds: dict | None) -> dict:
    t = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        t.update({k: v for k, v in thresholds.items() if k in DEFAULT_THRESHOLDS})
    return t


def is_weak(evidence: list[Evidence], *, thresholds: dict | None = None) -> bool:
    """True when fast retrieval is weak/contradictory and Level 2 is justified."""
    t = _merged(thresholds)
    if len(evidence) < t["min_results"]:
        return True
    top = evidence[0]
    if (top.scores.final or 0.0) < t["min_top_score"]:
        return True
    # Contradiction: top item ranked very differently by BM25 vs dense.
    if top.scores.bm25_rank is not None and top.scores.dense_rank is not None:
        if abs(top.scores.bm25_rank - top.scores.dense_rank) > t["max_rank_disagreement"]:
            return True
    return False
