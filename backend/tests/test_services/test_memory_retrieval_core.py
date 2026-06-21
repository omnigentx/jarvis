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
    Evidence, EvidenceScores, EvidenceSource, RetrievalRequest
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


def test_policy_does_not_override_relevance():
    # Regression (2026-06-16): a clearly-more-relevant but old/low-authority fact
    # must stay on top of a fresh + trusted but several-ranks-less-relevant one.
    # The bounded rank boost (≤ ~3) can't climb a 5-rank relevance gap — the
    # measured cause of an on-topic "ô tô" memory being pushed below off-topic
    # "user_confirmed" facts for a trip-planning query.
    now = 1_000_000.0
    rel = _ev("REL", authority="agent_observed", conf=0.5, ts=0.0)
    rel.scores.rrf = 0.020                          # most relevant (rank 0)
    fillers = []
    for i in range(4):
        f = _ev(f"F{i}", authority="agent_observed", conf=0.5, ts=0.0)
        f.scores.rrf = 0.018 - i * 0.001
        fillers.append(f)
    noise = _ev("NOISE", authority="user_confirmed", conf=0.9, ts=now)
    noise.scores.rrf = 0.010                        # rank 5 — far less relevant, but fresh + trusted
    out = fusion.apply_policy([noise, *fillers, rel], now=now)
    ids = [e.record_id for e in out]
    assert ids[0] == "REL"                          # relevance wins; boost can't climb 5 ranks
    assert ids.index("NOISE") >= 3                  # fresh+trusted noise stays down where relevance put it


def test_policy_recency_breaks_near_tie():
    # The flip side: among NEAR-equal-relevance same-topic facts, the newer one
    # is promoted (read-side of ADD-only). old=rank0, new=rank1 → new wins.
    now = 1_000_000.0
    old = _ev("OLD", authority="user_confirmed", conf=0.9, ts=now - 120 * 86400)
    old.scores.rrf = 0.0164
    new = _ev("NEW", authority="user_confirmed", conf=0.9, ts=now - 1 * 86400)
    new.scores.rrf = 0.0161                         # marginally less relevant, but fresh
    out = fusion.apply_policy([old, new], now=now)
    assert out[0].record_id == "NEW"                # recency promotes the newer fact past the near-tie


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


def _orch_settings():
    import types
    return types.SimpleNamespace(
        embedding_model="BAAI/bge-m3", embedding_revision="",
        evidence_token_budget=2500, trigger_lexicon_overrides={},
        quality_gate_thresholds={}, mode="balanced")


def test_index_revision_bumps_on_projection(db):
    # C-fix: a (re)projection (outbox row completing) must change the revision
    # so the cache doesn't keep serving a pre-backfill result for the same query.
    from core.database import MemoryIndexOutbox
    from services.retrieval.orchestrator import RetrievalOrchestrator
    orch = RetrievalOrchestrator(db, _orch_settings())
    r0 = orch._index_revision()
    db.add(MemoryIndexOutbox(event_type="memory_upsert", aggregate_id="m1",
                             aggregate_revision=1, status="done", attempt_count=0,
                             next_attempt_at=0.0, created_at=0.0, completed_at=1000.0))
    db.commit()
    r1 = orch._index_revision()
    assert r1 != r0                              # projection bumped the token
    row = db.query(MemoryIndexOutbox).one()
    row.completed_at = 2000.0
    db.commit()
    assert orch._index_revision() != r1          # a later projection bumps again


async def test_offtopic_gate_drops_fts_when_dense_returns_nothing(db):
    # Relevance gate ("retrieve-or-not"): with the dense lane HEALTHY but
    # returning nothing within the similarity threshold, the query is off-topic,
    # so the FTS lane's hits are incidental keyword matches — drop them so an
    # unrelated turn injects no memory. When dense is DOWN (not just empty), keep
    # FTS-only (degraded) so keyword / exact-identifier recall still works.
    from services.retrieval.orchestrator import RetrievalOrchestrator
    orch = RetrievalOrchestrator(db, _orch_settings())
    budget = build_budget("balanced", evidence_token_budget=2500)
    req = RetrievalRequest(owner_agent_name="J", query="capital of france")

    class _FtsHit:
        async def search(self, request, *, limit):
            return [_ev("spurious", bm25=1)]               # FTS matched a stray keyword

    class _DenseEmpty:
        def is_available(self): return True
        async def search(self, request, *, limit): return []   # nothing within threshold

    orch._fts, orch._dense = _FtsHit(), _DenseEmpty()
    fused, dense_failed = await orch._fast_round(req, budget)
    assert fused == [] and dense_failed is False           # off-topic → nothing injected

    class _DenseDown:
        def is_available(self): return False
        async def search(self, request, *, limit): return []
    orch._dense = _DenseDown()
    fused2, _ = await orch._fast_round(req, budget)
    assert len(fused2) == 1                                 # FTS-only kept when dense is down


def test_dense_unpopulated_flags_empty_graph_only(db):
    # A''-fix: dense available but graph empty AND contributed nothing → degraded
    # ("unpopulated"), but NOT when dense contributed or the graph has nodes.
    import types
    from services.retrieval.orchestrator import RetrievalOrchestrator
    orch = RetrievalOrchestrator(db, _orch_settings())

    def _dense(count):
        return types.SimpleNamespace(store=types.SimpleNamespace(count=lambda: count))

    orch._dense = _dense(0)
    assert orch._dense_unpopulated([_ev("A")]) is True       # empty graph, no dense rank
    orch._dense = _dense(5)
    assert orch._dense_unpopulated([_ev("A")]) is False      # graph has nodes
    orch._dense = _dense(0)
    assert orch._dense_unpopulated([_ev("B", dense=1)]) is False  # dense DID contribute
    orch._dense = types.SimpleNamespace(store=None)
    assert orch._dense_unpopulated([_ev("A")]) is False      # non-graph backend


def test_ledger_dedup():
    led = EvidenceLedger()
    a, b = _ev("A"), _ev("B")
    led.add(a, turn=1)
    fresh = led.dedup([a, b], turn=2)
    assert [e.record_id for e in fresh] == ["B"]
    assert led.has("A")              # keyed on record_id, not evidence_id


def test_ledger_dedups_same_record_across_providers():
    """B3 regression: the SAME memory surfaced via a different provider mix has
    a different evidence_id but the same record_id. The ledger must still treat
    it as already-injected, else the fact is injected into context twice."""
    led = EvidenceLedger()
    dense_hit = _ev("A")              # evidence_id "e:A", record_id "A"
    graph_hit = _ev("A")
    graph_hit.evidence_id = "A"      # bare/graph-style id, SAME record_id
    led.add(dense_hit, turn=1)
    assert led.dedup([graph_hit], turn=2) == []   # deduped despite differing evidence_id


# ── H3 + M1: relevance gate carve-outs ──────────────────────────────────────

class _FakeProvider:
    def __init__(self, hits, available=True):
        self._hits, self._available = hits, available
    def is_available(self): return self._available
    async def search(self, request, *, limit): return list(self._hits)


@pytest.mark.asyncio
async def test_bm25_first_keeps_fts_hit_when_dense_empty():
    # H3: an exact-identifier query (PROJ-42) is legitimately far from stored
    # memories in embedding space → dense returns [] even on-topic. The gate
    # must NOT drop the strong FTS match when bm25_first is set.
    from types import SimpleNamespace

    from services.retrieval.orchestrator import RetrievalOrchestrator
    orch = RetrievalOrchestrator.__new__(RetrievalOrchestrator)
    orch._fts = _FakeProvider([_ev("PROJ", bm25=0, excerpt="ticket PROJ-42 approved")])
    orch._dense = _FakeProvider([], available=True)        # healthy but empty
    orch.settings = SimpleNamespace(quality_gate_thresholds={})
    budget = SimpleNamespace(max_candidates_per_retriever=10, max_fused_candidates=10)
    req = RetrievalRequest(owner_agent_name="Jarvis", query="status of PROJ-42")

    gated, _ = await orch._fast_round(req, budget, bm25_first=False)
    assert gated == []                                     # default gate drops it
    kept, _ = await orch._fast_round(req, budget, bm25_first=True)
    assert [e.record_id for e in kept] == ["PROJ"]         # exact-identifier recall preserved


def test_safe_match_expr_drops_stopwords():
    # M1: function-word-only queries yield no FTS match (off-topic guarantee that
    # holds even when the dense lane is down and the gate can't run).
    from services.indexing.fts_index import _safe_match_expr
    assert _safe_match_expr("thời tiết gì") == '"thời" OR "tiết"'   # 'gì' dropped
    assert _safe_match_expr("gì là của") is None                    # all function words
    assert _safe_match_expr("the weather") == '"weather"'           # 'the' dropped
