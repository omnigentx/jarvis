"""WS04 orchestrator: Level 0 short-circuit, Level 1 FTS retrieval (dense
degraded), telemetry, ledger dedup."""

import types

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, EpisodicDocument, MemoryRecord, RetrievalRun
from services.indexing import fts_index
from services.retrieval.contracts import RetrievalRequest
from services.retrieval.ledger import EvidenceLedger
from services.retrieval.orchestrator import RetrievalOrchestrator, _CACHE


def _settings():
    # Dense disabled so the suite deterministically exercises the FTS
    # (degraded) path regardless of the dense backend.
    return types.SimpleNamespace(
        embedding_model="BAAI/bge-m3", embedding_revision="",
        evidence_token_budget=2500, trigger_lexicon_overrides={},
        quality_gate_thresholds={}
    )


@pytest.fixture()
def db():
    _CACHE.clear()
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.connect() as c:
        c.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
            "doc_kind UNINDEXED, doc_id UNINDEXED, owner_agent_name UNINDEXED, content)"))
        c.commit()
    s = sessionmaker(bind=engine)()
    # seed an owned memory + its FTS row
    s.add(MemoryRecord(id="m1", owner_agent_name="Jarvis", memory_type="semantic",
                       subject_scope="project:jarvis", content="use a dedicated compactor agent",
                       normalized_content="use a dedicated compactor agent", status="active",
                       authority="user_confirmed", confidence=0.9, current_version=1, created_at=1.0))
    fts_index.fts_upsert(s, doc_kind=fts_index.KIND_MEMORY, doc_id="m1",
                         owner_agent_name="Jarvis", content="use a dedicated compactor agent")
    s.commit()
    yield s
    s.close()


async def test_level0_short_circuits(db):
    orch = RetrievalOrchestrator(db, _settings())
    res = await orch.retrieve(RetrievalRequest(owner_agent_name="Jarvis", query="hi there"),
                              now=100.0)
    assert res.level == 0 and res.evidence == []
    # Level 0 writes no telemetry
    assert db.query(RetrievalRun).count() == 0


async def test_recency_ranks_newer_fact_first_on_happy_path(db):
    # ADD-only read-side (#3): two facts on the same topic; the NEWER one must
    # rank first on the FAST/happy path — recency now runs there, not only on
    # escalation. FTS-only path (dense unavailable), no embeddings needed.
    NOW = 1_000_000_000.0
    for rid, content, ca in [("old", "user works at Techcombank office", NOW - 120 * 86400),
                             ("new", "user works at FPT office", NOW - 1 * 86400)]:
        db.add(MemoryRecord(id=rid, owner_agent_name="Jarvis", memory_type="semantic",
                            subject_scope="user", content=content, normalized_content=content,
                            status="active", authority="user_confirmed", confidence=0.9,
                            current_version=1, created_at=ca))
        fts_index.fts_upsert(db, doc_kind=fts_index.KIND_MEMORY, doc_id=rid,
                             owner_agent_name="Jarvis", content=content)
    db.commit()
    orch = RetrievalOrchestrator(db, _settings())
    res = await orch.retrieve(RetrievalRequest(owner_agent_name="Jarvis", query="office"),
                              now=NOW, agent_requested=True)
    ids = [e.record_id for e in res.evidence]
    assert "new" in ids and "old" in ids
    assert ids.index("new") < ids.index("old")     # newer (FPT) outranks older (Techcombank)


async def test_level1_fts_retrieval_and_telemetry(db):
    orch = RetrievalOrchestrator(db, _settings())
    res = await orch.retrieve(
        RetrievalRequest(owner_agent_name="Jarvis", query="compactor"),
        now=100.0, agent_requested=True)        # force Level 1
    assert res.level == 1
    assert any(e.record_id == "m1" for e in res.evidence)
    assert all(e.owner_agent_name == "Jarvis" for e in res.evidence)
    assert res.degraded is True                 # dense unavailable in this env
    # telemetry row written
    run = db.query(RetrievalRun).one()
    assert run.owner_agent_name == "Jarvis" and run.mode == "balanced"


async def test_ledger_prevents_duplicate_injection(db):
    orch = RetrievalOrchestrator(db, _settings())
    led = EvidenceLedger()
    r1 = await orch.retrieve(RetrievalRequest(owner_agent_name="Jarvis", query="compactor"),
                             now=100.0, agent_requested=True, ledger=led, turn=1)
    assert r1.evidence
    r2 = await orch.retrieve(RetrievalRequest(owner_agent_name="Jarvis", query="compactor"),
                             now=200.0, agent_requested=True, ledger=led, turn=2)
    # already in ledger → not re-injected
    assert r2.evidence == []
