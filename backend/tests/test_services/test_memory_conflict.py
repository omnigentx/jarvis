"""WS09 conflict detection + resolution: embedding gate finds related memory,
curator decides supersede/merge/keep-both/reject; degrades to a deferred
conflict candidate when no curator."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, MemoryCandidate, MemoryRecord
from services.memory import conflict
from services.memory.curator import MemoryCurator
from services.memory.memory_service import MemoryService


class _FakeEmb:
    def is_available(self): return True
    def dim(self): return 2
    def revision(self): return "fake"
    def embed_query(self, t): return self._v(t)
    def embed_documents(self, ts): return [self._v(t) for t in ts]
    def _v(self, t):
        t = t.lower()
        if "pho" in t: return [1.0, 0.0]      # all pho statements cluster
        if "sushi" in t: return [0.0, 1.0]
        return [0.6, 0.6]


def _curator(action, merged="merged pho preference"):
    return MemoryCurator(lambda p: json.dumps({"action": action, "reason": "t",
                                               "merged_content": merged}))


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _seed(db, content="I like pho"):
    MemoryService(db).create_memory(owner_agent_name="Jarvis", memory_type="semantic",
                                    content=content, subject_scope="user",
                                    authority="user_confirmed", now=100.0)


def _active(db):
    return db.query(MemoryRecord).filter_by(status="active").all()


def test_no_similar_creates_normally(db):
    _seed(db, "I like pho")
    action = conflict.resolve_or_create(
        db, owner="Jarvis", memory_type="semantic", content="I like sushi",
        scope="user", authority="user_confirmed", now=200.0, embedding=_FakeEmb())
    assert action == "created"
    assert len(_active(db)) == 2          # unrelated → both kept


def test_supersede_on_reversal(db):
    _seed(db, "I like pho")
    action = conflict.resolve_or_create(
        db, owner="Jarvis", memory_type="semantic", content="I no longer like pho",
        scope="user", authority="user_confirmed", now=200.0,
        embedding=_FakeEmb(), curator=_curator("supersede"))
    assert action == "superseded"
    statuses = {r.content: r.status for r in db.query(MemoryRecord).all()}
    assert statuses["I like pho"] == "superseded"          # old preserved, not deleted
    assert statuses["I no longer like pho"] == "active"


def test_merge(db):
    _seed(db, "I like pho")
    action = conflict.resolve_or_create(
        db, owner="Jarvis", memory_type="semantic", content="but not southern pho",
        scope="user", authority="user_confirmed", now=200.0,
        embedding=_FakeEmb(), curator=_curator("merge"))
    assert action == "merged"
    active = _active(db)
    assert len(active) == 1 and "merged pho preference" in active[0].content


def test_keep_both_refinement(db):
    _seed(db, "I like pho")
    action = conflict.resolve_or_create(
        db, owner="Jarvis", memory_type="semantic", content="especially northern pho",
        scope="user", authority="user_confirmed", now=200.0,
        embedding=_FakeEmb(), curator=_curator("create"))
    assert action == "kept_both"
    assert len(_active(db)) == 2          # compatible refinement → both active


def test_reject(db):
    _seed(db, "I like pho")
    action = conflict.resolve_or_create(
        db, owner="Jarvis", memory_type="semantic", content="pho is food",
        scope="user", authority="user_confirmed", now=200.0,
        embedding=_FakeEmb(), curator=_curator("reject"))
    assert action == "rejected"
    assert len(_active(db)) == 1          # nothing new


def test_defer_when_no_curator(db):
    _seed(db, "I like pho")
    action = conflict.resolve_or_create(
        db, owner="Jarvis", memory_type="semantic", content="I dislike pho now",
        scope="user", authority="user_confirmed", now=200.0,
        embedding=_FakeEmb(), curator=None)      # no curator built in test
    assert action == "deferred"
    cand = db.query(MemoryCandidate).filter_by(candidate_type="conflict").one()
    p = json.loads(cand.payload_json)
    assert p["conflicts_with"] and p["existing_content"] == "I like pho"
    assert len(_active(db)) == 1          # new NOT created until resolved


def test_episodic_skips_conflict_check(db):
    _seed(db, "I like pho")
    # episodic memory_type is immutable history → no conflict detection
    action = conflict.resolve_or_create(
        db, owner="Jarvis", memory_type="episodic", content="I like pho",
        scope="user", authority="agent_observed", now=200.0, embedding=_FakeEmb())
    assert action == "created"
