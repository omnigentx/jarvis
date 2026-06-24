"""Reciprocal Rank Fusion + bounded deterministic policy re-ranking (spec §8.4).

Fuse BM25 and dense result lists by RANK (never raw-score addition), then nudge
the order by bounded RANK boosts (authority / recency / confidence). Boosts are
applied in rank space — not as multipliers on the compressed RRF score — so they
tune near-ties (a newer same-topic fact rises a couple of positions) without ever
letting a low-relevance result leapfrog genuinely-more-relevant evidence.
"""
from __future__ import annotations

import copy

from services.retrieval.contracts import Evidence

RRF_K = 60

# Policy nudges in RANK units (NOT score multipliers). Working in rank space is
# what keeps the nudge bounded: multiplying the RRF score let a +10% authority
# bonus flip a rank-7 result past a rank-3 one, because RRF compresses every rank
# into a ~0.0003-wide band (measured distortion 2026-06-16). A rank boost instead
# lets a newer / higher-authority same-topic fact climb a FEW positions without
# ever leapfrogging a clearly-more-relevant result several ranks above it.
_AUTHORITY_RANK_BOOST = {
    "tool_verified": 1.0,
    "user_confirmed": 0.5,
    "agent_observed": 0.0,
    "reported_by_agent": -0.3,
    "external_document": -0.3,
    "inferred": -0.6,
}


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
                if ev.scores.graph_rank is not None:        # keep graph provenance
                    cur.scores.graph_rank = ev.scores.graph_rank
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


def _recency_rank_boost(now: float, created_at: float | None) -> float:
    """How many ranks a fact climbs for being recent — the read-side of ADD-only
    ('works at NovaCorp' should outrank the older 'works at AcmeCorp' for the same
    query). Bounded so it tunes near-ties, never overrides clear relevance."""
    if not created_at:
        return 0.0
    age_days = max(0.0, (now - created_at) / 86400.0)
    if age_days <= 7:
        return 1.5
    if age_days <= 90:
        return 0.5
    return 0.0


def apply_policy(evidence: list[Evidence], *, now: float) -> list[Evidence]:
    """Re-rank by relevance, nudged by BOUNDED rank boosts (recency / authority /
    confidence). The boost (≤ ~3 ranks of climb) lets a newer or higher-authority
    same-topic fact rise above a marginally-more-relevant older one, while a
    fresh-but-off-topic memory still cannot leapfrog a result several ranks more
    relevant — fixing the 2026-06-16 distortion where a rank-7 "user_confirmed"
    fact outranked a rank-3 "agent_observed" one. ``scores.final`` keeps the
    relevance value for telemetry; the boosts only change ORDER.

    INTENTIONAL DIVERGENCE FROM SPEC §ranking: the spec models these as
    MULTIPLICATIVE score weights (``adjusted_score = rrf * authority_weight *
    confidence_weight * ...``). We deliberately use a BOUNDED additive RANK boost
    instead. Reason: a multiplicative confidence/authority weight can let a very
    confident but loosely-relevant memory scale its way above a clearly-more-
    relevant one — exactly the distortion above. A capped rank nudge guarantees
    relevance always dominates (a memory can only climb a few ranks, never
    leapfrog several), which is the property we actually want. The spec's intent
    ("bounded modifiers, not replacements for relevance") is preserved; only the
    mechanism differs. If aligning to the literal formula later, clamp the product
    so it cannot reorder beyond this bound.

    Supersession is NOT handled here: providers return only ``status='active'``
    rows (superseded/archived are filtered at query time).

    RERANKER OVERRIDE: once a cross-encoder reranker has scored the candidates
    (``scores.reranker`` set), THAT is the authoritative relevance order — it read
    (query, memory) jointly, far better than the bi-encoder ranks RRF fuses. So we
    sort by reranker and skip the rrf-based reorder (the bounded recency/authority
    nudges below are for the rrf regime; applying them on top of a cross-encoder
    score would just add noise)."""
    if any(e.scores.reranker is not None for e in evidence):
        for e in evidence:
            if e.scores.reranker is not None:
                e.scores.final = e.scores.reranker
        evidence.sort(key=lambda e: (e.scores.reranker if e.scores.reranker is not None
                                     else float("-inf")), reverse=True)
        return evidence
    evidence.sort(key=lambda e: e.scores.rrf or 0.0, reverse=True)   # relevance baseline

    def _boost(e) -> float:
        return (_recency_rank_boost(now, e.source.timestamp)
                + _AUTHORITY_RANK_BOOST.get(e.authority, 0.0)
                + (max(0.0, min(1.0, e.confidence)) - 0.5))          # confidence ±0.5 rank

    ranked = []
    for i, e in enumerate(evidence):
        e.scores.final = e.scores.rrf or 0.0
        ranked.append((i - _boost(e), -(e.source.timestamp or 0.0), i, e))
    # lower effective rank first; ties → newer first, then original relevance order.
    ranked.sort(key=lambda t: (t[0], t[1], t[2]))
    evidence[:] = [t[3] for t in ranked]
    return evidence
