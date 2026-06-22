"""Recall e2e through the REAL orchestrator: hook → _retrieve →
RetrievalOrchestrator → (LadybugProvider dense + SQLite FTS) → RRF → inject.

The ONLY stub is the embedding (deterministic fake); the orchestrator, both
providers, fusion, the LadybugDB graph, and the injection hook are all
production code. Isolated SQLite + temp LadybugDB.
"""
import tempfile
import types

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

pytest.importorskip("ladybug")
from core.database import Base, MemoryRecord  # noqa: E402
from services.indexing import fts_index  # noqa: E402
from services.indexing.ladybug_store import EMBED_DIM, LadybugStore  # noqa: E402
from services.memory import retrieval_hook as rh  # noqa: E402
from services.retrieval.orchestrator import _CACHE  # noqa: E402


class _FakeEmb:
    def is_available(self): return True
    def dim(self): return EMBED_DIM
    def revision(self): return "fake"
    def _v(self, t):
        v = [0.0] * EMBED_DIM
        v[0 if "fpt" in (t or "").lower() else 1] = 1.0
        return v
    def embed_documents(self, texts): return [self._v(t) for t in texts]
    def embed_query(self, q): return self._v(q)


def _cfg():
    return types.SimpleNamespace(
        enabled=True, auto_capture_preferences=False, mode="balanced",
        embedding_model="BAAI/bge-m3", embedding_revision="",
        ladybug_path="unused",
        evidence_token_budget=2500, trigger_lexicon_overrides={},
        quality_gate_thresholds={}, approval_policy="manual", pinned_token_budget=1500)


class _Agent:
    def __init__(self): self.name = "Jarvis"; self.message_history = []
    def load_message_history(self, m): self.message_history = list(m)


class _Runner:
    def __init__(self, a): self._agent = a


def _user(t):
    from fast_agent.mcp.helpers.content_helpers import text_content
    from fast_agent.mcp.prompt_message_extended import PromptMessageExtended
    return PromptMessageExtended(role="user", content=[text_content(t)])


@pytest.fixture()
def wired(monkeypatch):
    _CACHE.clear()
    eng = create_engine("sqlite:///:memory:",
                        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    with eng.connect() as c:
        c.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
                       "doc_kind UNINDEXED, doc_id UNINDEXED, owner_agent_name UNINDEXED, content)"))
        c.commit()
    F = sessionmaker(bind=eng)
    store = LadybugStore(f"{tempfile.mkdtemp()}/g")
    emb = _FakeEmb()

    # Seed one Jarvis memory + one Riley memory in BOTH the SoT(+FTS) and the graph.
    seed = F()
    for rid, owner, content in [("m1", "Jarvis", "user works at fpt"),
                                ("r1", "Riley [SA]", "riley works at fpt")]:
        seed.add(MemoryRecord(id=rid, owner_agent_name=owner, memory_type="semantic",
                              subject_scope="user", content=content, normalized_content=content,
                              status="active", authority="user_confirmed", confidence=0.9,
                              current_version=1, created_at=1.0))
        fts_index.fts_upsert(seed, doc_kind=fts_index.KIND_MEMORY, doc_id=rid,
                             owner_agent_name=owner, content=content)
        store.upsert_memory(record_id=rid, owner=owner, memory_type="semantic",
                            subject_scope="user", content=content, embedding=emb._v(content),
                            authority="user_confirmed", confidence=0.9, created_at=1.0, valid_from=1.0)
    seed.commit(); seed.close()

    import core.database as cd
    import services.indexing.ladybug_store as lbs
    import services.memory.settings as ms
    import services.retrieval.orchestrator as orch
    monkeypatch.setattr(cd, "get_db_session", lambda: F())
    monkeypatch.setattr(orch, "get_shared_embedding_provider", lambda *a, **k: emb)
    monkeypatch.setattr(lbs, "get_ladybug_store", lambda path: store)
    monkeypatch.setattr(ms, "get_memory_settings", lambda: _cfg())
    yield types.SimpleNamespace(agent=_Agent())
    store.close()


async def test_recall_injects_real_orchestrator_result(wired):
    agent = wired.agent
    hooks = rh.create_memory_retrieval_hooks()
    delta = [_user("where is the fpt office")]
    await hooks.before_llm_call(_Runner(agent), delta)
    agent.load_message_history(list(agent.message_history) + delta)   # framework merge
    injected = [m for m in agent.message_history if rh.is_injected_memory(m)]
    assert len(injected) == 1
    blk = rh._msg_text(injected[0])
    assert "fpt" in blk.lower()                      # the seeded memory surfaced
    # cross-agent isolation: Riley's memory must NOT be in Jarvis's recall.
    assert "riley" not in blk.lower()


# ── GraphRAG co-occurrence + relevance gate (real orchestrator, fake embed) ───

async def test_graphrag_query_anchored_and_relevance_gate(monkeypatch):
    """E2E through the REAL orchestrator (LadybugProvider dense + query-anchored
    graph + FTS + fusion + gate); only the embedder is faked.

    Proves the QUERY-ANCHORED GraphRAG contract (replaces blind seed co-occurrence,
    the 2026-06-22 'AI-career memory in a baby-age query' bug):
    1. A memory whose OWN embedding is far from the query is pulled via the graph
       ONLY when the query NAMES the shared entity ('acme').
    2. A query that does NOT name the entity ('plan a trip') does NOT drag that
       memory in — even though it shares an entity with a vector hit.
    3. Relevance gate — an off-topic query injects nothing.

    Six memories so 'acme' (df 2/6) is below the hub cut (≥3) and stays usable;
    fillers also give the off-topic query genuinely-empty vector results.
    """
    _CACHE.clear()
    eng = create_engine("sqlite:///:memory:",
                        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    with eng.connect() as c:
        c.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
                       "doc_kind UNINDEXED, doc_id UNINDEXED, owner_agent_name UNINDEXED, content)"))
        c.commit()
    F = sessionmaker(bind=eng)
    store = LadybugStore(f"{tempfile.mkdtemp()}/g")

    _AXIS = {"trip": 0, "address": 5, "milk": 1, "blue": 2, "jazz": 3, "tennis": 4}

    class _Emb:
        def is_available(self): return True
        def dim(self): return EMBED_DIM
        def revision(self): return "fake"
        def _v(self, t):
            t = (t or "").lower()
            v = [0.0] * EMBED_DIM
            v[next((ax for kw, ax in _AXIS.items() if kw in t), 9)] = 1.0
            return v
        def embed_documents(self, ts): return [self._v(t) for t in ts]
        def embed_query(self, q): return self._v(q)
    emb = _Emb()

    # A near "trip"; B vector-far ("address") and its CONTENT has no 'acme' token
    # (so B can ONLY arrive via the graph anchor, not FTS). Both MENTION entity
    # Acme. C–F are distinct fillers that dilute Acme's df below the hub cut.
    rows = [
        ("A", "trip planning notes", "acme"),
        ("B", "office address downtown", "acme"),   # NB: no 'acme' word in content
        ("C", "buy milk today", "grocery"),
        ("D", "the sky is blue", "color"),
        ("E", "jazz playlist", "music"),
        ("F", "tennis match", "sport"),
    ]
    seed = F()
    for rid, content, ent in rows:
        seed.add(MemoryRecord(id=rid, owner_agent_name="Jarvis", memory_type="semantic",
                              subject_scope="user", content=content, normalized_content=content,
                              status="active", authority="user_confirmed", confidence=0.9,
                              current_version=1, created_at=1.0))
        fts_index.fts_upsert(seed, doc_kind=fts_index.KIND_MEMORY, doc_id=rid,
                             owner_agent_name="Jarvis", content=content)
        store.upsert_memory(record_id=rid, owner="Jarvis", memory_type="semantic",
                            subject_scope="user", content=content, embedding=emb._v(content),
                            authority="user_confirmed", confidence=0.9, created_at=1.0, valid_from=1.0)
        store.link_entity(record_id=rid, entity_id=f"ent:{ent}", name=ent,
                          etype="org", normalized=ent)
    seed.commit(); seed.close()

    import core.database as cd
    import services.indexing.ladybug_store as lbs
    import services.memory.settings as ms
    import services.retrieval.orchestrator as orch_mod
    monkeypatch.setattr(cd, "get_db_session", lambda: F())
    monkeypatch.setattr(orch_mod, "get_shared_embedding_provider", lambda *a, **k: emb)
    monkeypatch.setattr(lbs, "get_ladybug_store", lambda path: store)
    monkeypatch.setattr(ms, "get_memory_settings", lambda: _cfg())

    import time

    from services.retrieval.contracts import RetrievalRequest
    from services.retrieval.orchestrator import RetrievalOrchestrator

    # 1. Query NAMES 'acme' → B (vector-far, content has no 'acme' word) is pulled
    #    ONLY via the graph anchor.
    res = await RetrievalOrchestrator(F(), _cfg()).retrieve(
        RetrievalRequest(owner_agent_name="Jarvis", query="tell me about acme"),
        now=time.time(), agent_requested=True)
    by_id = {e.record_id: e.scores for e in res.evidence}
    assert "B" in by_id
    assert by_id["B"].graph_rank is not None and by_id["B"].dense_rank is None

    # 2. Query does NOT name the entity → graph must NOT drag B in (the bug guard).
    _CACHE.clear()
    res2 = await RetrievalOrchestrator(F(), _cfg()).retrieve(
        RetrievalRequest(owner_agent_name="Jarvis", query="plan a trip"),
        now=time.time(), agent_requested=True)
    ids2 = {e.record_id for e in res2.evidence}
    assert "A" in ids2                     # direct vector hit
    assert "B" not in ids2                 # NOT pulled — query named no entity

    # 3. off-topic: vector-far from everything + names no entity → nothing.
    _CACHE.clear()
    res3 = await RetrievalOrchestrator(F(), _cfg()).retrieve(
        RetrievalRequest(owner_agent_name="Jarvis", query="quantum chromodynamics lecture"),
        now=time.time(), agent_requested=True)
    assert res3.evidence == []
    store.close()
