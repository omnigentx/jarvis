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

async def test_graphrag_cooccurrence_and_relevance_gate(monkeypatch):
    """E2E through the REAL orchestrator (LadybugProvider dense + linked_memories
    + FTS + fusion + gate); only the embedder is faked.

    Proves two behaviours end-to-end:
    1. GraphRAG co-occurrence — a memory that SHARES an entity with the vector hit
       is pulled in via MENTIONS even though its OWN embedding is far from the
       query (the value the dense gate would otherwise miss).
    2. Relevance gate — an off-topic query (vector-far from everything) injects
       nothing.
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

    class _Emb:
        def is_available(self): return True
        def dim(self): return EMBED_DIM
        def revision(self): return "fake"
        def _v(self, t):
            t = (t or "").lower()
            v = [0.0] * EMBED_DIM
            v[0 if "trip" in t else 5 if "address" in t else 9] = 1.0
            return v
        def embed_documents(self, ts): return [self._v(t) for t in ts]
        def embed_query(self, q): return self._v(q)
    emb = _Emb()

    # A is near the query ("trip"); B is far ("address"); BOTH mention "Acme".
    seed = F()
    for rid, content in [("A", "trip planning notes"), ("B", "acme office address")]:
        seed.add(MemoryRecord(id=rid, owner_agent_name="Jarvis", memory_type="semantic",
                              subject_scope="user", content=content, normalized_content=content,
                              status="active", authority="user_confirmed", confidence=0.9,
                              current_version=1, created_at=1.0))
        fts_index.fts_upsert(seed, doc_kind=fts_index.KIND_MEMORY, doc_id=rid,
                             owner_agent_name="Jarvis", content=content)
        store.upsert_memory(record_id=rid, owner="Jarvis", memory_type="semantic",
                            subject_scope="user", content=content, embedding=emb._v(content),
                            authority="user_confirmed", confidence=0.9, created_at=1.0, valid_from=1.0)
        store.link_entity(record_id=rid, entity_id="ent:acme", name="Acme",
                          etype="org", normalized="acme")     # shared entity → MENTIONS
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

    # 1. on-topic: vector hits A; B is pulled via shared-entity MENTIONS (Lane C).
    res = await RetrievalOrchestrator(F(), _cfg()).retrieve(
        RetrievalRequest(owner_agent_name="Jarvis", query="plan a trip"),
        now=time.time(), agent_requested=True)
    ids = {e.record_id for e in res.evidence}
    assert "A" in ids                      # direct vector hit
    assert "B" in ids                      # GraphRAG co-occurrence (vector-far, shared entity)
    # Lane PROVENANCE (powers the debug UI): A came from the vector lane, B ONLY
    # from the graph (MENTIONS) lane — so the UI can show graph's unique pull.
    by_id = {e.record_id: e.scores for e in res.evidence}
    assert by_id["A"].dense_rank is not None and by_id["A"].graph_rank is None
    assert by_id["B"].graph_rank is not None and by_id["B"].dense_rank is None

    # 2. off-topic: vector-far from everything → relevance gate → nothing injected.
    _CACHE.clear()
    res2 = await RetrievalOrchestrator(F(), _cfg()).retrieve(
        RetrievalRequest(owner_agent_name="Jarvis", query="quantum chromodynamics lecture"),
        now=time.time(), agent_requested=True)
    assert res2.evidence == []
    store.close()
