"""Reciprocal Rank Fusion + bounded deterministic policy weighting (spec §8.4).

Fuse BM25 and dense result lists by RANK (never raw-score addition), then
apply bounded policy multipliers (authority / freshness / confidence /
status). The multipliers are clamped so a low-relevance result can never
outrank all genuine retrieval evidence — they nudge, they don't replace
relevance.
"""
from __future__ import annotations

import copy

from services.retrieval.contracts import Evidence

RRF_K = 60

# Per-factor multiplier bounds (kept tight so relevance dominates).
_AUTHORITY_WEIGHT = {
    "tool_verified": 1.15,
    "user_confirmed": 1.10,
    "agent_observed": 1.0,
    "reported_by_agent": 0.95,
    "external_document": 0.95,
    "inferred": 0.85,
}
_STATUS_WEIGHT = {"active": 1.0, "superseded": 0.6, "archived": 0.5}


def rrf_fuse(result_lists: list[list[Evidence]], *, k: int = RRF_K) -> list[Evidence]:
    """Fuse ranked lists by record_id. Each list is assumed ordered best-first.
    Merges BM25/dense ranks of the same record into one Evidence and sets
    ``scores.rrf`` + ``scores.final`` (= rrf before policy)."""
    scores: dict[str, float] = {}
    merged: dict[str, Evidence] = {}
    for lst in result_lists:
        for rank, ev in enumerate(lst, start=1):
            rid = ev.record_id
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank)
            if rid not in merged:
                merged[rid] = copy.deepcopy(ev)
            else:
                cur = merged[rid]
                if ev.scores.bm25_rank is not None:
                    cur.scores.bm25_rank = ev.scores.bm25_rank
                if ev.scores.dense_rank is not None:
                    cur.scores.dense_rank = ev.scores.dense_rank
                # prefer a non-empty excerpt
                if not cur.excerpt and ev.excerpt:
                    cur.excerpt = ev.excerpt
    out: list[Evidence] = []
    for rid, ev in merged.items():
        ev.scores.rrf = scores[rid]
        ev.scores.final = scores[rid]
        out.append(ev)
    out.sort(key=lambda e: e.scores.rrf, reverse=True)
    return out


def _freshness_weight(now: float, created_at: float | None) -> float:
    """Mild recency nudge in [0.9, 1.1]; old but relevant memory still wins."""
    if not created_at:
        return 1.0
    age_days = max(0.0, (now - created_at) / 86400.0)
    if age_days <= 7:
        return 1.1
    if age_days <= 90:
        return 1.0
    return 0.9


def apply_policy(evidence: list[Evidence], *, now: float) -> list[Evidence]:
    """Apply bounded policy multipliers to ``scores.final`` and re-sort."""
    for ev in evidence:
        w = (
            _AUTHORITY_WEIGHT.get(ev.authority, 1.0)
            * _STATUS_WEIGHT.get(_status_of(ev), 1.0)
            * _freshness_weight(now, ev.source.timestamp)
            * (0.9 + 0.2 * max(0.0, min(1.0, ev.confidence)))  # confidence in [0.9,1.1]
        )
        ev.scores.final = (ev.scores.rrf or 0.0) * w
    # Newer wins ties: the day-bucketed freshness nudge can't separate two facts
    # captured in the SAME session (e.g. "works at Techcombank" then "works at
    # FPT"), so break score ties by recency — the read-side of ADD-only.
    evidence.sort(key=lambda e: (e.scores.final, e.source.timestamp or 0.0), reverse=True)
    return evidence


def _status_of(ev: Evidence) -> str:
    # status isn't on Evidence directly; default active (providers only return
    # active records). Hook kept for when superseded items are surfaced.
    return "active"
