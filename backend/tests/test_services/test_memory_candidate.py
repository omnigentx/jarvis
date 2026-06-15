"""WS05 candidate lifecycle + curator: deterministic auto-approve, approval
gating, dedupe, secret→approval, approve/reject, curator decision parsing."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, MemoryCandidate, MemoryRecord
from services.memory import candidate_service as cnd


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _payload(content="answer in Vietnamese", **kw):
    base = dict(memory_type="pinned", content=content, subject_scope="user",
                authority="user_confirmed")
    base.update(kw)
    return base


def test_deterministic_candidate_auto_persists(db):
    c = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="preference",
                             payload=_payload(), now=100.0)
    assert c.status == "auto_approved"
    assert db.query(MemoryRecord).filter_by(owner_agent_name="Jarvis").count() == 1


def test_requires_approval_stays_pending(db):
    c = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="fact",
                             payload=_payload(content="we picked Postgres"), now=100.0,
                             requires_approval=True)
    assert c.status == "pending"
    assert db.query(MemoryRecord).count() == 0          # nothing persisted yet


def test_approve_then_persists(db):
    c = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="fact",
                             payload=_payload(content="we picked Postgres",
                                              memory_type="semantic", subject_scope="project:jarvis"),
                             now=100.0, requires_approval=True)
    cnd.approve_candidate(db, c.id, now=200.0)
    assert db.get(MemoryCandidate, c.id).status == "approved"
    assert db.query(MemoryRecord).filter_by(owner_agent_name="Jarvis").count() == 1


def test_reject_does_not_persist(db):
    c = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="fact",
                             payload=_payload(content="maybe wrong"), now=100.0,
                             requires_approval=True)
    cnd.reject_candidate(db, c.id, now=200.0, reason="not durable")
    assert db.get(MemoryCandidate, c.id).status == "rejected"
    assert db.query(MemoryRecord).count() == 0


def test_dedupe_returns_existing(db):
    a = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="fact",
                             payload=_payload(content="dup note"), now=100.0,
                             requires_approval=True)
    b = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="fact",
                             payload=_payload(content="dup   NOTE"), now=200.0,
                             requires_approval=True)
    assert a.id == b.id
    assert db.query(MemoryCandidate).count() == 1


def test_secret_forces_approval(db):
    c = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="fact",
                             payload=_payload(content="key is sk-ABCDEF0123456789ABCDEF",
                                              memory_type="semantic", subject_scope="system"),
                             now=100.0)  # NOT requires_approval, but secret forces it
    assert c.status == "pending" and c.requires_approval == 1
    assert db.query(MemoryRecord).count() == 0


def test_compactor_ingest_manual_policy_pending(db):
    cands = [{"type": "semantic", "subtype": "architecture_decision",
              "content": "Use a dedicated compactor agent.", "subject_scope": "project:jarvis",
              "source_message_indexes": [28, 29], "confidence": 0.97, "explicit": True}]
    ids = cnd.ingest_compactor_candidates(db, owner_agent_name="Jarvis", candidates=cands,
                                          now=100.0, approval_policy="manual")
    assert len(ids) == 1
    assert db.get(MemoryCandidate, ids[0]).status == "pending"   # manual → awaits approval
    assert db.query(MemoryRecord).count() == 0


def test_compactor_ingest_auto_policy_persists(db):
    cands = [{"type": "semantic", "content": "We chose Postgres over MySQL.",
              "subject_scope": "project:jarvis", "confidence": 0.95, "explicit": True}]
    ids = cnd.ingest_compactor_candidates(db, owner_agent_name="Jarvis", candidates=cands,
                                          now=100.0, approval_policy="auto_low_risk")
    assert db.get(MemoryCandidate, ids[0]).status == "auto_approved"
    assert db.query(MemoryRecord).filter_by(owner_agent_name="Jarvis").count() == 1
