"""WS03 outbox: idempotency, claim/lease, backoff, dead-letter, recovery."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, MemoryIndexOutbox
from services.indexing import outbox_service as ob


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _enq(db, rev=1, now=100.0, agg="rec-1", etype=ob.EVENT_MEMORY_UPSERT):
    ok = ob.enqueue(db, event_type=etype, aggregate_id=agg,
                    aggregate_revision=rev, now=now)
    db.commit()
    return ok


def test_enqueue_idempotent_on_same_revision(db):
    assert _enq(db, rev=1) is True
    assert _enq(db, rev=1) is False           # duplicate → no-op
    assert _enq(db, rev=2) is True            # new revision → enqueued
    assert db.query(MemoryIndexOutbox).count() == 2


def test_next_deadline_min_of_pending_retry_and_lease(db):
    # The event-driven worker sleeps until the nearest deadline absent a notify:
    # the next pending retry OR an expired-lease reclaim of a stuck in_progress row.
    db.add(MemoryIndexOutbox(event_type="memory_upsert", aggregate_id="a", aggregate_revision=1,
                             status=ob.PENDING, attempt_count=0, next_attempt_at=500.0, created_at=0.0))
    db.add(MemoryIndexOutbox(event_type="memory_upsert", aggregate_id="b", aggregate_revision=1,
                             status=ob.IN_PROGRESS, attempt_count=0, next_attempt_at=0.0,
                             lease_expires_at=300.0, created_at=0.0))
    db.commit()
    assert ob.next_deadline(db, now=100.0) == 300.0      # lease reclaim is sooner
    db.query(MemoryIndexOutbox).filter_by(aggregate_id="b").delete(); db.commit()
    assert ob.next_deadline(db, now=100.0) == 500.0      # only the pending retry remains
    db.query(MemoryIndexOutbox).delete(); db.commit()
    assert ob.next_deadline(db, now=100.0) is None        # nothing outstanding → idle


def test_notify_fires_after_commit_only_never_on_rollback(db):
    fired = []
    ob.set_notifier(lambda: fired.append(1))
    try:
        ob.enqueue(db, event_type=ob.EVENT_MEMORY_UPSERT, aggregate_id="x",
                   aggregate_revision=1, now=1.0)
        assert fired == []                  # pre-commit: NOT yet (worker mustn't see uncommitted)
        db.commit()
        assert fired == [1]                 # post-commit → exactly one wake

        ob.enqueue(db, event_type=ob.EVENT_MEMORY_UPSERT, aggregate_id="y",
                   aggregate_revision=1, now=1.0)
        db.rollback()
        assert fired == [1]                 # rolled back → no phantom wake
    finally:
        ob.set_notifier(None)


def test_enqueue_force_requeues_done_row(db):
    # Regression (2026-06-16): a backend switch (Qdrant→LadybugDB) left old
    # memories whose outbox row was already ``done`` permanently unprojected,
    # because plain re-enqueue is a no-op on the UNIQUE (event,agg,rev) row.
    # ``force=True`` (used by consistency_service.rebuild / the v2 migration)
    # must reset that done row to pending so the worker re-projects it.
    assert _enq(db, rev=1) is True
    row = db.query(MemoryIndexOutbox).one()
    ob.mark_done(db, row.id, now=200.0)
    db.commit()
    assert db.get(MemoryIndexOutbox, row.id).status == ob.DONE

    assert _enq(db, rev=1) is False           # plain re-enqueue still a no-op
    forced = ob.enqueue(db, event_type=ob.EVENT_MEMORY_UPSERT, aggregate_id="rec-1",
                        aggregate_revision=1, now=300.0, force=True)
    db.commit()
    assert forced is True
    fresh = db.get(MemoryIndexOutbox, row.id)
    assert fresh.status == ob.PENDING         # re-queued, not skipped
    assert fresh.next_attempt_at == 300.0
    assert fresh.completed_at is None
    assert db.query(MemoryIndexOutbox).count() == 1   # reset in place, not duplicated


def test_claim_batch_only_due_pending_ordered(db):
    _enq(db, rev=1, now=100.0)
    _enq(db, rev=2, now=50.0)                  # earlier next_attempt
    _enq(db, rev=3, now=200.0)                 # not due at now=120
    claimed = ob.claim_batch(db, limit=10, now=120.0)
    revs = [r.aggregate_revision for r in claimed]
    assert revs == [2, 1]                      # due ones, ordered by next_attempt
    assert all(r.status == ob.IN_PROGRESS for r in claimed)
    assert all(r.lease_expires_at == 120.0 + ob.DEFAULT_LEASE_SECONDS for r in claimed)


def test_mark_done(db):
    _enq(db, rev=1)
    [row] = ob.claim_batch(db, limit=1, now=120.0)
    ob.mark_done(db, row.id, now=130.0)
    fresh = db.get(MemoryIndexOutbox, row.id)
    assert fresh.status == ob.DONE and fresh.completed_at == 130.0
    assert fresh.lease_expires_at is None


def test_mark_failed_backoff_then_dead(db):
    _enq(db, rev=1)
    [row] = ob.claim_batch(db, limit=1, now=120.0)
    status = ob.mark_failed(db, row.id, error="boom", now=120.0,
                            max_attempts=3, base_backoff_s=5.0)
    fresh = db.get(MemoryIndexOutbox, row.id)
    assert status == ob.PENDING
    assert fresh.attempt_count == 1
    assert fresh.next_attempt_at == 125.0      # 5 * 2^0
    assert fresh.last_error == "boom"
    # second failure
    ob.mark_failed(db, row.id, error="boom2", now=200.0, max_attempts=3, base_backoff_s=5.0)
    assert db.get(MemoryIndexOutbox, row.id).next_attempt_at == 210.0  # 5 * 2^1
    # third failure → dead-letter
    status = ob.mark_failed(db, row.id, error="boom3", now=300.0, max_attempts=3, base_backoff_s=5.0)
    assert status == ob.DEAD
    assert db.get(MemoryIndexOutbox, row.id).status == ob.DEAD


def test_reclaim_expired_leases(db):
    _enq(db, rev=1)
    [row] = ob.claim_batch(db, limit=1, now=120.0)  # lease expires at 120+300=420
    # before expiry → nothing reclaimed
    assert ob.reclaim_expired_leases(db, now=400.0) == 0
    # after expiry → back to pending
    assert ob.reclaim_expired_leases(db, now=500.0) == 1
    fresh = db.get(MemoryIndexOutbox, row.id)
    assert fresh.status == ob.PENDING and fresh.lease_expires_at is None


def test_stats_counts_by_status(db):
    _enq(db, rev=1); _enq(db, rev=2); _enq(db, rev=3)
    [r] = ob.claim_batch(db, limit=1, now=120.0)
    ob.mark_done(db, r.id, now=130.0)
    counts = ob.stats(db)
    assert counts.get(ob.PENDING) == 2
    assert counts.get(ob.DONE) == 1
