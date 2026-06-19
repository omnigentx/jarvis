"""END-TO-END memory flows through PRODUCTION code (no mocking of the memory
subsystem). One real SQLite DB (+FTS5) shared across MemoryService, the index
worker, the retrieval orchestrator, and the agent-facing RPC handlers —
exactly the path a live request takes, minus the dense lane (LadybugDB + embeddings) (absent here,
so these prove the FTS degraded path + no-lost-writes guarantee).

Covers spec §26: remember→recall, exact-identifier BM25, cross-agent denial,
episodic recall, dense-outage resilience.
"""
import types

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, MemoryIndexOutbox, MemoryRecord
from services.indexing import outbox_service as ob
from services.indexing import projector
from services.indexing import memory_index_worker as wmod
from services.indexing.memory_index_worker import MemoryIndexWorker
from services.memory import rpc_handlers
from services.memory.memory_service import MemoryService
from services.retrieval.orchestrator import _CACHE


def _settings():
    # These e2e flows assert the FTS/degraded path and the no-lost-writes
    # guarantee. The dense lane (LadybugDB) is forced unavailable in the `stack`
    # fixture so this runs FTS-only deterministically (no model load / no store).
    return types.SimpleNamespace(
        enabled=True, embedding_model="BAAI/bge-m3",
        embedding_revision="", evidence_token_budget=2500,
        trigger_lexicon_overrides={}, quality_gate_thresholds={},
        approval_policy="auto_low_risk", pinned_token_budget=2000)


@pytest.fixture()
def stack(monkeypatch):
    """Wire one shared DB across every production component."""
    _CACHE.clear()
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.connect() as c:
        c.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
            "doc_kind UNINDEXED, doc_id UNINDEXED, owner_agent_name UNINDEXED, content)"))
        c.commit()
    Factory = sessionmaker(bind=engine)
    # worker + rpc open their own sessions → point them at the shared engine
    monkeypatch.setattr(wmod, "SessionLocal", Factory)
    monkeypatch.setattr(rpc_handlers, "get_db_session", lambda: Factory())
    monkeypatch.setattr(rpc_handlers, "get_memory_settings", _settings)
    import services.memory.settings as ms
    monkeypatch.setattr(ms, "get_memory_settings", _settings)
    # Force the dense lane (LadybugDB) UNAVAILABLE so this e2e deterministically
    # exercises the FTS/degraded path: store=None → is_available() False → the
    # worker defers dense (FTS still written) and the orchestrator recalls
    # FTS-only — no embedding model load, no real graph store opened.
    import services.indexing.ladybug_store as lbs
    def _no_store(*_a, **_k):
        raise RuntimeError("dense disabled for this e2e")
    monkeypatch.setattr(lbs, "get_ladybug_store", _no_store)
    return types.SimpleNamespace(
        Factory=Factory, worker=MemoryIndexWorker())


import time as _time


async def _drain(worker, now=None):
    # Far-future clock so rows written with fixed test timestamps OR real
    # time.time() (the RPC paths use real time) are always due. Deferred dense
    # rows just re-pend (no dense lane in tests); FTS is written on the first pass.
    return await worker.process_pending(now=_time.time() + 100_000)


async def test_remember_then_recall_through_full_pipeline(stack):
    # 1. agent/user creates a durable memory via the write authority
    svc = MemoryService(stack.Factory(), pinned_token_budget=2000)
    rec = svc.create_memory(
        owner_agent_name="Jarvis", memory_type="semantic",
        content="We decided to use a dedicated compactor agent for context.",
        subject_scope="project:jarvis", authority="user_confirmed", now=100.0)

    # 2. the background worker drains the outbox into the (FTS) index
    stats = await _drain(stack.worker, now=110.0)
    assert stats["done"] + stats["deferred"] >= 1     # FTS updated (dense deferred: no LadybugDB)

    # 3. the agent searches via the real RPC handler → real orchestrator → FTS
    res = await rpc_handlers.memory_search(agent_name="Jarvis", query="dedicated compactor")
    ids = {e["id"] for e in res["memories"]}          # id = "memory:<record_id>"
    assert any(i.endswith(rec.id) for i in ids)
    assert res.get("degraded") is True                # Qdrant absent → degraded but works

    # 4. progressive disclosure: fetch full content
    full = await rpc_handlers.memory_fetch(agent_name="Jarvis",
                                           evidence_ids=[f"memory:{rec.id}"])
    assert "dedicated compactor" in full["items"][0]["content"]


async def test_exact_identifier_recalled_via_bm25(stack):
    svc = MemoryService(stack.Factory())
    rec = svc.create_memory(
        owner_agent_name="Jarvis", memory_type="episodic",
        content="The crash was a NULL deref in backend/services/foo.py at line 42.",
        subject_scope="project:jarvis", authority="agent_observed", now=100.0)
    await _drain(stack.worker, now=110.0)
    res = await rpc_handlers.memory_search(agent_name="Jarvis", query="backend/services/foo.py")
    assert any(e["id"].endswith(rec.id) for e in res["memories"])


async def test_cross_agent_memory_denied_end_to_end(stack):
    svc = MemoryService(stack.Factory())
    svc.create_memory(owner_agent_name="Jarvis", memory_type="semantic",
                      content="Jarvis private architecture note about caching.",
                      subject_scope="project:jarvis", authority="user_confirmed", now=100.0)
    await _drain(stack.worker, now=110.0)
    # another agent must not retrieve it
    res = await rpc_handlers.memory_search(agent_name="Riley [SA]", query="architecture caching")
    assert res["memories"] == []


async def test_episodic_projection_recall(stack):
    db = stack.Factory()
    projector.project_and_enqueue(
        db, owner_agent_name="Jarvis", document_type="message", source_id="sess:1",
        content="Yesterday we deployed via the staged verification flow.", now=100.0)
    db.commit(); db.close()
    await _drain(stack.worker, now=110.0)
    res = await rpc_handlers.memory_search(agent_name="Jarvis", query="staged verification deploy")
    assert any("verification" in e["text"] for e in res["memories"])


async def test_dense_outage_keeps_writes_and_chat(stack):
    """No dense lane: writes still persist, the outbox retains the dense intent for
    later, and search still returns results via FTS (degraded, not broken)."""
    svc = MemoryService(stack.Factory())
    rec = svc.create_memory(owner_agent_name="Jarvis", memory_type="semantic",
                            content="resilience note: outbox keeps dense work pending",
                            subject_scope="system", authority="tool_verified", now=100.0)
    await _drain(stack.worker, now=110.0)
    # write survived + dense intent still pending (deferred), not lost / dead
    db = stack.Factory()
    row = db.query(MemoryIndexOutbox).filter_by(aggregate_id=rec.id).first()
    assert row.status == ob.PENDING and row.attempt_count == 0
    db.close()
    res = await rpc_handlers.memory_search(agent_name="Jarvis", query="resilience outbox")
    assert any(e["id"].endswith(rec.id) for e in res["memories"])


async def test_agent_remember_tool_persists_and_recalls(stack):
    # auto_low_risk policy → explicit remember auto-saves
    out = await rpc_handlers.memory_remember(
        agent_name="Jarvis", content="The staging DB host is db-stage.internal",
        memory_type="semantic", subject_scope="project:jarvis")
    assert out["status"] == "auto_approved"
    await _drain(stack.worker, now=110.0)
    res = await rpc_handlers.memory_search(agent_name="Jarvis", query="staging DB host")
    assert any("db-stage" in e["text"] for e in res["memories"])


async def test_agent_forget_archives_and_drops_from_search(stack):
    svc = MemoryService(stack.Factory())
    rec = svc.create_memory(owner_agent_name="Jarvis", memory_type="semantic",
                            content="temporary note to forget about caching",
                            subject_scope="system", authority="agent_observed", now=100.0)
    await _drain(stack.worker)
    assert (await rpc_handlers.memory_search(agent_name="Jarvis", query="caching"))["memories"]
    # forget → archive. Retrieval excludes non-active memory immediately (the
    # provider filters status), and the outbox also queues FTS/dense removal.
    out = await rpc_handlers.memory_forget(agent_name="Jarvis", memory_id=rec.id)
    assert out["status"] == "archived"
    db = stack.Factory()
    assert db.get(MemoryRecord, rec.id).status == "archived"     # persisted
    db.close()
    res = await rpc_handlers.memory_search(agent_name="Jarvis", query="caching")
    assert not any(e["id"].endswith(rec.id) for e in res["memories"])


async def test_email_capture_recall_is_participant_scoped(stack):
    from services.memory.communication_capture import capture_email
    db = stack.Factory()
    capture_email(db, sender="Jarvis", recipients=["Riley [SA]"],
                  subject="deploy window", body="we deploy Friday 9pm via the staged flow",
                  now=100.0)
    db.close()
    await _drain(stack.worker, now=110.0)
    # sender recalls it
    jarvis = await rpc_handlers.memory_search(agent_name="Jarvis", query="deploy window staged")
    assert any("deploy" in e["text"].lower() for e in jarvis["memories"])
    # recipient recalls it
    riley = await rpc_handlers.memory_search(agent_name="Riley [SA]", query="deploy window staged")
    assert riley["memories"]
    # a non-participant does not
    other = await rpc_handlers.memory_search(agent_name="Other", query="deploy window staged")
    assert other["memories"] == []
