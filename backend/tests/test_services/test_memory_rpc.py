"""WS04 memory RPC handlers: owner scoping (no cross-agent leak), fetch auth."""

import types

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, CommunicationRecord, MemoryRecord
from services.indexing import fts_index
from services.memory import rpc_handlers
from services.retrieval.orchestrator import _CACHE


def _settings():
    return types.SimpleNamespace(
        enabled=True,
        qdrant_url="http://localhost:59999", vector_backend="qdrant", embedding_model="BAAI/bge-m3",
        embedding_revision="", evidence_token_budget=2500,
        trigger_lexicon_overrides={}, quality_gate_thresholds={},
        approval_policy="auto_low_risk", pinned_token_budget=1500)


@pytest.fixture()
def wired(monkeypatch):
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
    seed = Factory()
    seed.add(MemoryRecord(id="m1", owner_agent_name="Jarvis", memory_type="semantic",
                          subject_scope="project:jarvis", content="jarvis private compactor note",
                          normalized_content="jarvis private compactor note", status="active",
                          authority="user_confirmed", confidence=0.9, current_version=1, created_at=1.0))
    seed.add(MemoryRecord(id="r1", owner_agent_name="Riley [SA]", memory_type="semantic",
                          subject_scope="project:jarvis", content="riley private compactor note",
                          normalized_content="riley private compactor note", status="active",
                          authority="user_confirmed", confidence=0.9, current_version=1, created_at=1.0))
    fts_index.fts_upsert(seed, doc_kind=fts_index.KIND_MEMORY, doc_id="m1",
                         owner_agent_name="Jarvis", content="jarvis private compactor note")
    fts_index.fts_upsert(seed, doc_kind=fts_index.KIND_MEMORY, doc_id="r1",
                         owner_agent_name="Riley [SA]", content="riley private compactor note")
    seed.commit(); seed.close()

    monkeypatch.setattr(rpc_handlers, "get_db_session", lambda: Factory())
    monkeypatch.setattr(rpc_handlers, "get_memory_settings", _settings)
    return Factory


async def test_search_is_owner_scoped(wired):
    jarvis = await rpc_handlers.memory_search(agent_name="Jarvis", query="compactor")
    ids = {e["record_id"] for e in jarvis["evidence"]}
    assert "m1" in ids and "r1" not in ids       # never sees Riley's memory

    riley = await rpc_handlers.memory_search(agent_name="Riley [SA]", query="compactor")
    rids = {e["record_id"] for e in riley["evidence"]}
    assert "r1" in rids and "m1" not in rids


async def test_search_requires_bound_identity(wired):
    res = await rpc_handlers.memory_search(agent_name="", query="x")
    assert "error" in res


async def test_remember_normalizes_llm_guessed_scope(wired):
    # Regression: the LLM passed subject_scope="user_profile" (not in the
    # taxonomy). The tool used to ERROR, so the memory was never saved while
    # the agent claimed success. It must normalize to a valid scope and save.
    out = await rpc_handlers.memory_remember(
        agent_name="Jarvis", content="user prefers travelling by car",
        memory_type="semantic", subject_scope="user_profile")
    assert "error" not in out
    assert out.get("candidate_id")


async def test_remember_defaults_scope_to_user(wired):
    out = await rpc_handlers.memory_remember(
        agent_name="Jarvis", content="user likes pho", memory_type="semantic")
    assert "error" not in out and out.get("candidate_id")


async def test_fetch_is_owner_scoped(wired):
    ok = await rpc_handlers.memory_fetch(agent_name="Jarvis", evidence_ids=["memory:m1"])
    assert ok["items"] and ok["items"][0]["content"].startswith("jarvis")
    # Jarvis cannot fetch Riley's memory even with the raw id
    denied = await rpc_handlers.memory_fetch(agent_name="Jarvis", evidence_ids=["memory:r1"])
    assert denied["items"] == []


async def test_fetch_comm_evidence_authorized(wired):
    """N2 regression: graph/comm evidence uses the canonical comm:{id} scheme;
    memory_fetch must resolve it (kind 'comm' was previously unhandled → the
    'view source' fetch returned empty) and re-check participant authorization."""
    Factory = wired
    db = Factory()
    db.add(CommunicationRecord(id="c1", sender="Riley [SA]", recipients_json='["Jarvis"]',
                               channel="email", subject="Plan", body="ship it Monday",
                               created_at=1.0))
    db.commit()
    db.close()

    ok = await rpc_handlers.memory_fetch(agent_name="Jarvis", evidence_ids=["comm:c1"])
    assert ok["items"] and "ship it Monday" in ok["items"][0]["content"]
    # A non-participant cannot fetch it even with the raw id.
    denied = await rpc_handlers.memory_fetch(agent_name="Mallory", evidence_ids=["comm:c1"])
    assert denied["items"] == []
