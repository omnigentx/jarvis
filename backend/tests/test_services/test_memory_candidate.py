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


@pytest.fixture()
def saved_events(monkeypatch):
    """Capture the live `memory_saved` SSE the chat chip consumes."""
    import services.activity_stream as asm
    evs = []
    monkeypatch.setattr(asm.activity_stream_manager, "broadcast",
                        lambda ev: evs.append(ev))
    return evs                        # live list, grows as events fire


def _saved(evs):
    return [e for e in evs if e.get("event_type") == "memory_saved"]


def test_memory_saved_event_auto(db, saved_events):
    c = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="preference",
                             payload=_payload(), now=100.0)
    saved = _saved(saved_events)
    assert len(saved) == 1
    d = saved[0]["data"]
    assert d["status"] == "saved" and d["candidate_id"] == c.id and d["record_id"]
    assert d["content"] == "answer in Vietnamese" and d["sensitive"] is False


def test_memory_saved_event_pending_then_approved(db, saved_events):
    c = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="fact",
                             payload=_payload(content="we picked Postgres", memory_type="semantic"),
                             now=100.0, requires_approval=True)
    saved = _saved(saved_events)
    assert [e["data"]["status"] for e in saved] == ["pending"]
    assert saved[0]["data"]["record_id"] is None      # not active yet
    cnd.approve_candidate(db, c.id, now=200.0)
    saved = _saved(saved_events)
    assert saved[-1]["data"]["status"] == "saved" and saved[-1]["data"]["record_id"]
    assert saved[-1]["data"]["candidate_id"] == c.id   # SAME id → chip updates in place


def test_memory_saved_event_rejected(db, saved_events):
    c = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="fact",
                             payload=_payload(content="we picked Postgres", memory_type="semantic"),
                             now=100.0, requires_approval=True)
    cnd.reject_candidate(db, c.id, now=200.0)
    assert _saved(saved_events)[-1]["data"]["status"] == "rejected"


def test_memory_saved_event_masks_secret(db, saved_events):
    # A secret forces approval (pending) AND must never ride the SSE in cleartext.
    cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="fact",
                         payload=_payload(content="db password: hunter2secret"), now=100.0)
    d = _saved(saved_events)[0]["data"]
    assert d["status"] == "pending" and d["sensitive"] is True
    assert "hunter2secret" not in d["content"] and "🔒" in d["content"]


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


def test_dedupe_collapses_across_lanes(db):
    # RC1: the same fact proposed by the agent's `remember` tool and by the
    # background extractor must collapse to ONE card — candidate_type is NOT
    # part of the dedupe key, only (owner, subject_scope, normalized content).
    a = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="agent_remember",
                             payload=_payload(content="user commutes at 6:50"), now=100.0,
                             requires_approval=True)
    b = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="extracted",
                             payload=_payload(content="user commutes at 6:50"), now=200.0,
                             requires_approval=True)
    assert a.id == b.id
    assert db.query(MemoryCandidate).count() == 1


def test_dedupe_race_falls_back_to_winner_under_unique_index(db, monkeypatch):
    # H1: under real concurrency the two lanes both pass the read-check and both
    # INSERT. The partial UNIQUE index (installed by init_db) turns the loser's
    # INSERT into an IntegrityError, which must resolve to the winner — NOT a
    # second card, NOT a 500. Simulate the lost race by making the loser's
    # read-check miss once while the winner already exists.
    from sqlalchemy import text
    db.execute(text(
        "CREATE UNIQUE INDEX uq_candidate_open_dedup ON memory_candidates(dedupe_key) "
        "WHERE status IN ('pending','auto_approved','approved') AND dedupe_key IS NOT NULL"))
    db.commit()

    winner = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="agent_remember",
                                  payload=_payload(content="user commutes at 6:50"),
                                  now=100.0, requires_approval=True)

    real_find = cnd._find_open_dup
    calls = {"n": 0}
    def flaky_find(session, dedupe):       # miss on the loser's read-check, real afterwards
        calls["n"] += 1
        return None if calls["n"] == 1 else real_find(session, dedupe)
    monkeypatch.setattr(cnd, "_find_open_dup", flaky_find)

    loser = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="extracted",
                                 payload=_payload(content="user commutes at 6:50"),
                                 now=200.0, requires_approval=True)

    assert loser.id == winner.id                       # resolved to the winner
    assert db.query(MemoryCandidate).filter(
        MemoryCandidate.status == 'pending').count() == 1   # exactly one card


def test_dedupe_keeps_distinct_subject_scope(db):
    # A user-fact and an agent-observation of the same text are different
    # memories → different scope → NOT collapsed.
    a = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="fact",
                             payload=_payload(content="likes pho", subject_scope="user"),
                             now=100.0, requires_approval=True)
    b = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="fact",
                             payload=_payload(content="likes pho", subject_scope="agent:Jarvis"),
                             now=200.0, requires_approval=True)
    assert a.id != b.id
    assert db.query(MemoryCandidate).count() == 2


def test_secret_forces_approval(db):
    c = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="fact",
                             payload=_payload(content="key is sk-ABCDEF0123456789ABCDEF",
                                              memory_type="semantic", subject_scope="system"),
                             now=100.0)  # NOT requires_approval, but secret forces it
    assert c.status == "pending" and c.requires_approval == 1
    assert db.query(MemoryRecord).count() == 0


def test_approved_secret_persists(db):
    """B1 regression: a user-approved SECRET must persist. Secrets force
    approval; on the human-approved path _persist_from_candidate authorizes the
    write (allow_secret=True) instead of letting MemoryService raise and the
    error get swallowed — which silently dropped the explicitly-approved secret
    and diverged candidate/card state."""
    c = cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="fact",
                             payload=_payload(content="key is sk-ABCDEF0123456789ABCDEF",
                                              memory_type="semantic", subject_scope="system"),
                             now=100.0)
    assert c.status == "pending" and c.requires_approval == 1
    cnd.approve_candidate(db, c.id, now=200.0)               # must NOT raise
    assert db.get(MemoryCandidate, c.id).status == "approved"
    recs = db.query(MemoryRecord).filter_by(owner_agent_name="Jarvis").all()
    assert len(recs) == 1
    assert "sk-ABCDEF" in recs[0].content
    assert recs[0].sensitivity == "secret"


def test_compactor_subtype_forwarded_on_persist(db):
    """B2 regression: the compactor's subtype must survive persistence — it was
    stored on the candidate but dropped at the create_memory boundary."""
    cands = [{"type": "semantic", "subtype": "architecture_decision",
              "content": "We chose Postgres over MySQL.", "subject_scope": "project:jarvis",
              "confidence": 0.95, "explicit": True}]
    cnd.ingest_compactor_candidates(db, owner_agent_name="Jarvis", candidates=cands,
                                    now=100.0, approval_policy="auto_low_risk")
    rec = db.query(MemoryRecord).filter_by(owner_agent_name="Jarvis").one()
    assert rec.memory_subtype == "architecture_decision"


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


def test_confidence_flows_from_candidate_to_memory(db):
    """Regression: confidence must reach the persisted memory, not default to 0.5.
    The extracted/fast lane carries it in the payload; the compaction lane sets
    the candidate column. _persist_from_candidate prefers payload, falls back to
    the column. Before the fix every memory defaulted to 0.5 and the relevance
    policy's (confidence - 0.5) rank-boost was a permanent no-op."""
    # payload-carried (fast/extracted lane): real LLM value wins over the column.
    cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="extracted",
                         payload=_payload(content="user works at fpt",
                                          memory_type="semantic", confidence=0.95), now=100.0)
    rec_a = db.query(MemoryRecord).filter_by(content="user works at fpt").one()
    assert rec_a.confidence == 0.95

    # column-carried (compaction lane): payload has no confidence → use the column.
    cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="preference",
                         payload=_payload(content="user likes dark mode"),
                         confidence=0.8, now=200.0)
    rec_b = db.query(MemoryRecord).filter_by(content="user likes dark mode").one()
    assert rec_b.confidence == 0.8


def test_confidence_metadata_reaches_version(db):
    """How confidence was derived (method + signals) is recorded in
    MemoryVersion.metadata_json → auditable + migratable when the formula changes."""
    import json as _json

    from core.database import MemoryVersion
    cnd.create_candidate(
        db, owner_agent_name="Jarvis", candidate_type="extracted",
        payload=_payload(content="user works at fpt", memory_type="semantic",
                         confidence=0.9, confidence_method="evidence_alignment_v1:direct",
                         reasoning_type="direct", excerpt_ok=True),
        confidence=0.9, now=100.0)
    rec = db.query(MemoryRecord).filter_by(content="user works at fpt").one()
    v = db.query(MemoryVersion).filter_by(memory_id=rec.id, version=1).one()
    meta = _json.loads(v.metadata_json)
    assert meta["confidence_method"] == "evidence_alignment_v1:direct"
    assert meta["reasoning_type"] == "direct" and meta["excerpt_ok"] is True


def test_approval_reason_codes(db):
    from services.memory.candidate_service import approval_reason
    assert approval_reason({"excerpt_ok": False}, "x") == "unverified_evidence"
    assert approval_reason({"excerpt_ok": True}, "x") is None
    assert approval_reason({}, "x") is None
    assert approval_reason({"excerpt_ok": True}, "password: hunter2secret") == "secret"


async def test_saved_sse_conversation_id_survives_create_task(monkeypatch):
    """#2 saved-path: the fast-lane extractor is spawned via asyncio.create_task
    DURING the turn, so it must SNAPSHOT current_conversation_id and still emit it
    after the chat route's `finally` resets the var. The recall test covers the
    synchronous path; this guards the cross-task hop — if the extractor is ever
    refactored to a persistent worker/thread the snapshot breaks and the saved
    chip silently mis-routes, so this must fail loudly."""
    import asyncio
    import types

    import services.activity_stream as as_mod
    from services.sse_progress import current_conversation_id

    captured = []
    monkeypatch.setattr(as_mod.activity_stream_manager, "broadcast",
                        lambda ev: captured.append(ev))
    cand = types.SimpleNamespace(
        id="cand1", owner_agent_name="Jarvis",
        payload_json='{"content": "User likes tea", "memory_type": "semantic"}',
        resolved_at=None, created_at=123.0,
    )

    async def _emit(): cnd._emit_saved(cand, status="saved")

    tok = current_conversation_id.set("conv-SAVED")
    task = asyncio.create_task(_emit())        # snapshots the ContextVar at creation
    current_conversation_id.reset(tok)         # the route turn ends BEFORE the task runs
    await task

    saved = [e for e in captured if e.get("event_type") == "memory_saved"]
    assert saved, "a memory_saved event was broadcast"
    assert saved[0]["data"]["conversation_id"] == "conv-SAVED"
