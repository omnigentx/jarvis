"""WS05 MemoryService — the write authority. Validation, dedupe, sensitivity,
pinned budget, version chain, archive/delete/rollback, owner enforcement."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, MemoryIndexOutbox, MemoryRecord, MemoryVersion
from services.indexing import outbox_service as ob
from services.memory.memory_service import MemoryService, MemoryWriteError


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture()
def svc(db):
    return MemoryService(db, pinned_token_budget=100)


def _create(svc, **kw):
    base = dict(owner_agent_name="Jarvis", memory_type="semantic",
                content="use a dedicated compactor agent", subject_scope="project:jarvis",
                authority="user_confirmed", confidence=0.5, now=100.0)
    base.update(kw)
    return svc.create_memory(**base)


def test_create_writes_record_version_and_outbox(svc, db):
    rec = _create(svc)
    assert rec.status == "active" and rec.current_version == 1
    assert db.query(MemoryVersion).filter_by(memory_id=rec.id, version=1).count() == 1
    row = db.query(MemoryIndexOutbox).one()
    assert row.event_type == ob.EVENT_MEMORY_UPSERT and row.aggregate_id == rec.id


def test_exact_duplicate_is_noop(svc, db):
    a = _create(svc)
    b = _create(svc, content="use a   dedicated COMPACTOR agent")  # normalized equal
    assert a.id == b.id
    assert db.query(MemoryRecord).count() == 1


def test_secret_content_rejected(svc):
    with pytest.raises(MemoryWriteError, match="secret"):
        _create(svc, content="the api key is sk-ABCDEF0123456789ABCDEF")


def test_pii_is_sensitive_persists_but_not_pinnable(svc):
    # PII (email) → SENSITIVE: unlike a SECRET it IS persisted, but the
    # sensitive tier still blocks auto-pinning.
    rec = _create(svc, content="reach me at jane.doe@example.com")
    assert rec.sensitivity == "sensitive"
    with pytest.raises(MemoryWriteError, match="sensitive"):
        _create(svc, content="card 4111 1111 1111 1111", pinned=True, subject_scope="user")


def test_invalid_scope_rejected(svc):
    with pytest.raises(ValueError):
        _create(svc, subject_scope="team:foo")


def test_pinned_budget_enforced(svc):
    # budget=100 tokens; first pin ok, second pushes over
    _create(svc, content="word " * 60, pinned=True)        # ~75 tokens
    with pytest.raises(MemoryWriteError, match="budget"):
        _create(svc, content="word " * 60, pinned=True, subject_scope="user")


def test_inferred_cannot_pin(svc):
    with pytest.raises(MemoryWriteError, match="cannot be pinned"):
        _create(svc, authority="inferred", pinned=True)


def test_update_bumps_version_and_reenqueues(svc, db):
    rec = _create(svc)
    svc.update_content(rec.id, "use TWO compactors", owner_agent_name="Jarvis", now=200.0)
    fresh = db.get(MemoryRecord, rec.id)
    assert fresh.current_version == 2 and "TWO" in fresh.content
    assert db.query(MemoryVersion).filter_by(memory_id=rec.id).count() == 2
    assert db.query(MemoryIndexOutbox).count() == 2          # v1 + v2


def test_archive_sets_status_and_enqueues_delete(svc, db):
    rec = _create(svc)
    svc.archive_memory(rec.id, owner_agent_name="Jarvis", now=200.0)
    assert db.get(MemoryRecord, rec.id).status == "archived"
    assert db.query(MemoryIndexOutbox).filter_by(event_type=ob.EVENT_MEMORY_DELETE).count() == 1


def test_restore_reactivates_and_reindexes(svc, db):
    # Restore is the reverse of archive: status → active AND an UPSERT (not DELETE)
    # so the worker re-projects it into the search index + graph.
    rec = _create(svc)
    svc.archive_memory(rec.id, owner_agent_name="Jarvis", now=200.0)
    svc.restore_memory(rec.id, owner_agent_name="Jarvis", now=300.0)
    assert db.get(MemoryRecord, rec.id).status == "active"
    # the most recent intent for this record is an UPSERT (re-index), not a delete.
    last = (db.query(MemoryIndexOutbox).filter_by(aggregate_id=rec.id)
            .order_by(MemoryIndexOutbox.id.desc()).first())
    assert last.event_type == ob.EVENT_MEMORY_UPSERT


def test_rollback_restores_old_content(svc, db):
    rec = _create(svc)
    svc.update_content(rec.id, "v2 content", owner_agent_name="Jarvis", now=200.0)
    svc.rollback_memory(rec.id, 1, owner_agent_name="Jarvis", now=300.0)
    fresh = db.get(MemoryRecord, rec.id)
    assert "compactor" in fresh.content and fresh.current_version == 3


def test_owner_enforcement(svc):
    rec = _create(svc)
    with pytest.raises(MemoryWriteError, match="not found"):
        svc.update_content(rec.id, "x", owner_agent_name="Riley [SA]", now=200.0)
