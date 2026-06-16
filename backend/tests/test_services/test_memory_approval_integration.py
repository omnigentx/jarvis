"""Integration: memory candidates surface in the UNIFIED Approvals inbox, and
resolution stays in sync BOTH ways (SSoT). Exercises the REAL
create_approval / resolve_approval (no mocks) so the candidate↔approval
boundary is actually covered.

Regression for two bugs the old best-effort swallow hid:
  - _create_approval_row sent no 'content' → KeyError → the card was silently
    never created → the user couldn't discover pending memories from the
    sidebar badge / Approvals page (only the agent's Memory tab).
  - the default create_approval PAUSES the requesting agent → an in-process
    Jarvis would FREEZE over a memory proposal (must be pause=False).
  - resolving on the Memory page left the inbox card 'pending' → stale badge
    (reverse sync was missing).
"""
import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, ApprovalRequestModel, MemoryCandidate
from services.memory import candidate_service as cnd


@pytest.fixture()
def wired(monkeypatch):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.connect() as c:
        c.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
            "doc_kind UNINDEXED, doc_id UNINDEXED, owner_agent_name UNINDEXED, content)"))
        c.commit()
    Factory = sessionmaker(bind=engine)
    # approval_service binds ``SessionLocal`` at import → patch the bound name
    # so its own sessions hit this in-memory engine (shared with the candidate).
    monkeypatch.setattr("services.approval_service.SessionLocal", Factory)
    return Factory


def _candidate(db):
    return cnd.create_candidate(
        db, owner_agent_name="Jarvis", candidate_type="agent_remember",
        payload={"memory_type": "semantic", "content": "user travels by car",
                 "subject_scope": "user", "authority": "agent_observed"},
        requires_approval=True)


def test_candidate_creates_inbox_card_with_content(wired):
    db = wired()
    cand = _candidate(db)
    db.close()
    chk = wired()
    cards = chk.query(ApprovalRequestModel).filter_by(approval_type="memory_candidate").all()
    assert len(cards) == 1, "memory candidate did not surface in the unified inbox"
    card = cards[0]
    assert card.content == "user travels by car"          # content present → no KeyError
    assert card.status == "pending"
    assert json.loads(card.metadata_json)["candidate_id"] == cand.id
    assert card.paused_agents == "[]"                      # pause=False → agent NOT frozen
    chk.close()


def test_resolve_on_memory_page_closes_card(wired):
    """Reverse sync: rejecting on the Memory page closes the inbox card in
    lockstep, so the sidebar badge doesn't go stale. Must NOT infinite-loop."""
    db = wired()
    cand = _candidate(db)
    cnd.reject_candidate(db, cand.id, reason="not useful")   # Memory-page path
    db.close()
    chk = wired()
    card = chk.query(ApprovalRequestModel).filter_by(approval_type="memory_candidate").one()
    assert card.status == "rejected"
    assert chk.get(MemoryCandidate, cand.id).status == "rejected"
    chk.close()


def test_resolve_from_inbox_updates_candidate(wired):
    """Forward sync: resolving the card in the inbox resolves the candidate."""
    from services.approval_service import approval_service
    db = wired()
    cand = _candidate(db)
    db.close()
    chk = wired()
    card_id = chk.query(ApprovalRequestModel).filter_by(approval_type="memory_candidate").one().id
    chk.close()
    approval_service.resolve_approval(card_id, "reject")
    final = wired()
    assert final.get(MemoryCandidate, cand.id).status == "rejected"
    assert final.query(ApprovalRequestModel).filter_by(id=card_id).one().status == "rejected"
    final.close()
