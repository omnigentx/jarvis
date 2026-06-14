"""WS04 retrieval core: RRF fusion, policy, FTS provider (owner-scoped),
evidence budget, cache, ledger."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, EpisodicDocument, MemoryRecord
from services.indexing import fts_index
from services.retrieval import cache as cache_mod
from services.retrieval import fusion
from services.retrieval.budget import build_budget
from services.retrieval.contracts import (
    Evidence, EvidenceScores, EvidenceSource, RetrievalRequest,
)
from services.retrieval.evidence_builder import build_evidence
from services.retrieval.ledger import EvidenceLedger
from services.retrieval.providers.sqlite_fts_provider import SqliteFtsProvider


def _ev(rid, *, bm25=None, dense=None, excerpt="x", authority="user_confirmed",
        conf=0.9, ts=0.0):
    return Evidence(f"e:{rid}", rid, "Jarvis", "semantic", excerpt,
                    EvidenceSource("session_message", rid, ts),
                    EvidenceScores(bm25_rank=bm25, dense_rank=dense),
                    authority, conf)


# ── fusion ──

def test_rrf_rewards_agreement_and_merges_ranks():
    bm25 = [_ev("A", bm25=1), _ev("B", bm25=2), _ev("C", bm25=3)]
    dense = [_ev("B", dense=1), _ev("A", dense=2), _ev("D", dense=3)]
    fused = fusion.rrf_fuse([bm25, dense])
    # A and B appear in both lists → top two
    assert {fused[0].record_id, fused[1].record_id} == {"A", "B"}
    top = fused[0]
    assert top.scores.bm25_rank is not None and top.scores.dense_rank is not None
    assert top.scores.rrf == top.scores.final


def test_policy_weights_bounded():
    a = _ev("A", bm25=1)          # user_confirmed
    b = _ev("B", bm25=2, authority="inferred")
    fused = fusion.rrf_fuse([[a, b]])
    fusion.apply_policy(fused, now=1000.0)
    # inferred is down-weighted but ranking is bounded; A still on top
    assert fused[0].record_id == "A"


# ── FTS provider ──

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.connect() as c:
        c.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
            "doc_kind UNINDEXED, doc_id UNINDEXED, owner_agent_name UNINDEXED, content)"))
        c.commit()
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


async def test_fts_provider_owner_scoped(db):
    db.add(MemoryRecord(id="m1", owner_agent_name="Jarvis", memory_type="semantic",
                        subject_scope="project:jarvis", content="use dedicated compactor",
                        normalized_content="use dedicated compactor", status="active",
                        authority="user_confirmed", confidence=0.9, current_version=1))
    db.add(EpisodicDocument(id="d1", owner_agent_name="Jarvis", document_type="message",
                            source_id="s:1", content="we deployed via the verification flow",
                            content_hash="h", created_at=1.0))
    fts_index.fts_upsert(db, doc_kind=fts_index.KIND_MEMORY, doc_id="m1",
                         owner_agent_name="Jarvis", content="use dedicated compactor")
    fts_index.fts_upsert(db, doc_kind=fts_index.KIND_EPISODIC, doc_id="d1",
                         owner_agent_name="Jarvis", content="we deployed via the verification flow")
    # another agent's doc must never surface for Jarvis
    fts_index.fts_upsert(db, doc_kind=fts_index.KIND_MEMORY, doc_id="x1",
                         owner_agent_name="Riley [SA]", content="use dedicated compactor")
    db.commit()

    prov = SqliteFtsProvider(db)
    res = await prov.search(RetrievalRequest(owner_agent_name="Jarvis", query="dedicated compactor"),
                            limit=10)
    rids = {e.record_id for e in res}
    assert "m1" in rids and "x1" not in rids
    assert all(e.owner_agent_name == "Jarvis" for e in res)
    assert all(e.scores.bm25_rank is not None for e in res)


async def test_fts_provider_type_filter(db):
    db.add(EpisodicDocument(id="d1", owner_agent_name="J", document_type="message",
                            source_id="s", content="alpha beta", content_hash="h", created_at=1.0))
    fts_index.fts_upsert(db, doc_kind=fts_index.KIND_EPISODIC, doc_id="d1",
                         owner_agent_name="J", content="alpha beta")
    db.commit()
    prov = SqliteFtsProvider(db)
    # restrict to semantic → episodic doc filtered out
    res = await prov.search(RetrievalRequest(owner_agent_name="J", query="alpha", types=["semantic"]),
                            limit=10)
    assert res == []


# ── evidence builder / cache / ledger ──

def test_evidence_builder_caps_items_and_tokens():
    ev = [_ev(str(i), excerpt="word " * 100) for i in range(10)]
    for i, e in enumerate(ev):
        e.scores.final = 1.0 - i * 0.01
    budget = build_budget("balanced")
    budget.max_evidence_items = 3
    selected, tokens = build_evidence(ev, budget)
    assert len(selected) == 3
    assert tokens > 0


def test_cache_key_changes_with_revision():
    k1 = cache_mod.cache_key(owner_agent_name="J", normalized_query="q",
                             filters="f", index_revision=1)
    k2 = cache_mod.cache_key(owner_agent_name="J", normalized_query="q",
                             filters="f", index_revision=2)
    assert k1 != k2
    c = cache_mod.RetrievalCache()
    c.set(k1, [_ev("A")])
    assert c.get(k1) is not None
    assert c.get(k2) is None          # different revision → miss


def test_ledger_dedup():
    led = EvidenceLedger()
    a, b = _ev("A"), _ev("B")
    led.add(a, turn=1)
    fresh = led.dedup([a, b], turn=2)
    assert [e.record_id for e in fresh] == ["B"]
    assert led.has("e:A")
