"""Cross-encoder rerank stage: re-orders the fused candidates by joint
(query, memory) relevance and drops those below the floor. The bi-encoder lanes
can't do this (the 2026-06-22 "cat memory in a baby-age query" case). Logic is
tested with a FAKE reranker — no model load."""
import types

from services.retrieval import fusion
from services.retrieval.contracts import (
    Evidence, EvidenceScores, EvidenceSource, RetrievalRequest)
from services.retrieval.orchestrator import RetrievalOrchestrator


def _ev(rid, rrf, content):
    return Evidence(f"m:{rid}", rid, "J", "semantic", content,
                    EvidenceSource("memory_record", rid, 1.0),
                    EvidenceScores(rrf=rrf, final=rrf), "user_confirmed", 0.9)


class _FakeRR:
    """Scores 1.0 when 'baby' is in the doc, else 0.0 — a stand-in for the
    cross-encoder's joint relevance judgement."""
    def is_available(self): return True
    def rerank(self, query, docs): return [1.0 if "baby" in d else 0.0 for d in docs]


def _stub(reranker, *, floor=0.5, top_k=20):
    return types.SimpleNamespace(
        settings=types.SimpleNamespace(rerank_top_k=top_k, rerank_min_score=floor),
        _reranker=reranker)


def test_rerank_reorders_and_drops_below_floor():
    # 'cat' noise outranks the relevant memory by RRF; the reranker must flip it
    # AND drop the noise (score 0.0 < floor).
    fused = [_ev("noise", 0.05, "user bought a cat mochi"),
             _ev("rel", 0.01, "user has a baby 7 months old")]
    out = RetrievalOrchestrator._apply_rerank(
        _stub(_FakeRR()), RetrievalRequest(owner_agent_name="J", query="how old is my baby"), fused)
    assert [e.record_id for e in out] == ["rel"]      # noise dropped, relevant kept
    assert out[0].scores.reranker == 1.0


def test_rerank_noop_when_unavailable():
    fused = [_ev("a", 0.05, "x")]
    out = RetrievalOrchestrator._apply_rerank(
        _stub(None), RetrievalRequest(owner_agent_name="J", query="q"), fused)
    assert out is fused                               # untouched → fusion order kept


def test_rerank_failure_keeps_fusion_order():
    class _Boom:
        def is_available(self): return True
        def rerank(self, q, docs): raise RuntimeError("model died")
    fused = [_ev("a", 0.05, "x"), _ev("b", 0.01, "y")]
    out = RetrievalOrchestrator._apply_rerank(
        _stub(_Boom()), RetrievalRequest(owner_agent_name="J", query="q"), fused)
    assert out is fused                               # best-effort: never break recall


def test_apply_policy_orders_by_reranker_over_rrf():
    a = _ev("a", 0.9, "x"); a.scores.reranker = 0.1   # high rrf, low rerank
    b = _ev("b", 0.1, "y"); b.scores.reranker = 0.9   # low rrf, high rerank
    out = fusion.apply_policy([a, b], now=1000.0)
    assert [e.record_id for e in out] == ["b", "a"]   # reranker is authoritative
    assert out[0].scores.final == 0.9
