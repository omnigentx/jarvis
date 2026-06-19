"""Transactional outbox for memory indexing (spec §5, §19; docs/memory-impl/03).

The outbox is the DURABLE QUEUE. Index intents are written in the SAME SQLite
transaction as the domain write, so "truth committed" and "index scheduled"
are atomic — Qdrant can be down for hours and nothing is lost. The worker
drains pending rows; idempotency comes from the
``UNIQUE(event_type, aggregate_id, aggregate_revision)`` constraint.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from sqlalchemy import event, func, text
from sqlalchemy.orm import Session

from core.database import MemoryIndexOutbox, SessionLocal

logger = logging.getLogger("memory.outbox")

# ── post-commit notify (event-driven worker wake) ────────────────────────────
# The notify is a best-effort ACCELERATOR only: the durable outbox is the source
# of truth, so a lost notify never loses work (the worker's startup drain + the
# retry/lease deadlines recover it). We wake AFTER a successful commit (never on
# rollback, never pre-commit) so the worker — which drains on its own SQLite
# connection — can't observe a not-yet-committed row.
_notifier: Optional[Callable[[], None]] = None
_DIRTY = "_outbox_dirty"          # session.info flag: this txn enqueued an intent


def set_notifier(fn: Callable[[], None] | None) -> None:
    """Register the worker's wake callback (idempotent; pass None to clear)."""
    global _notifier
    _notifier = fn


def notify() -> None:
    fn = _notifier
    if fn is None:
        return
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — a wake must never break a commit
        logger.warning("[MEMORY] outbox notify failed: %s", exc)


@event.listens_for(Session, "after_commit")
def _outbox_after_commit(session) -> None:
    if session.info.pop(_DIRTY, False):
        notify()


@event.listens_for(Session, "after_rollback")
def _outbox_after_rollback(session) -> None:
    session.info.pop(_DIRTY, False)


def next_deadline(db: Session, *, now: float) -> float | None:
    """Earliest time the worker MUST wake even without a notify: the next pending
    retry (``next_attempt_at``) or an expired-lease reclaim (``lease_expires_at``
    of an in_progress row left by a crash). ``None`` when nothing is outstanding
    → the worker idles until the next notify (or a safety watchdog)."""
    pend = (db.query(func.min(MemoryIndexOutbox.next_attempt_at))
            .filter(MemoryIndexOutbox.status == PENDING).scalar())
    lease = (db.query(func.min(MemoryIndexOutbox.lease_expires_at))
             .filter(MemoryIndexOutbox.status == IN_PROGRESS,
                     MemoryIndexOutbox.lease_expires_at.isnot(None)).scalar())
    cands = [t for t in (pend, lease) if t is not None]
    return min(cands) if cands else None

# Event types.
EVENT_MEMORY_UPSERT = "memory_upsert"
EVENT_MEMORY_DELETE = "memory_delete"
EVENT_EPISODIC_UPSERT = "episodic_upsert"
EVENT_EPISODIC_PRUNE = "episodic_prune"
EVENT_REBUILD_ALL = "rebuild_all"

# Status values.
PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
DEAD = "dead"

DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_BASE_BACKOFF_S = 5.0
DEFAULT_LEASE_SECONDS = 300.0


def enqueue(
    db: Session,
    *,
    event_type: str,
    aggregate_id: str,
    aggregate_revision: int,
    payload_json: str = "{}",
    now: float,
    force: bool = False,
) -> bool:
    """Add an index intent. MUST be called inside the caller's domain
    transaction (same ``db``) so it commits atomically with the write.

    Idempotent: re-enqueue of an already-present (event, aggregate, revision)
    is a no-op (returns False). The UNIQUE constraint is the backstop; we
    query first to avoid poisoning the caller's transaction with an
    IntegrityError.

    ``force=True`` is for re-projection (migration / full rebuild): when a row
    for this (event, aggregate, revision) already exists — typically status
    ``done`` from a prior index pass — reset it to PENDING so the worker
    re-processes it. The UNIQUE constraint forbids a second row, so a plain
    enqueue would skip it forever; that is exactly why a Qdrant→LadybugDB
    backend switch left old (already-``done``) memories unprojected. Returns
    True when it (re)queues work.
    """
    exists = (
        db.query(MemoryIndexOutbox)
        .filter(
            MemoryIndexOutbox.event_type == event_type,
            MemoryIndexOutbox.aggregate_id == aggregate_id,
            MemoryIndexOutbox.aggregate_revision == aggregate_revision,
        )
        .first()
    )
    if exists:
        if not force:
            return False
        exists.status = PENDING
        exists.attempt_count = 0
        exists.next_attempt_at = now
        exists.last_error = None
        exists.lease_expires_at = None
        exists.completed_at = None
        db.info[_DIRTY] = True            # wake the worker after this txn commits
        return True
    db.add(MemoryIndexOutbox(
        event_type=event_type,
        aggregate_id=aggregate_id,
        aggregate_revision=aggregate_revision,
        payload_json=payload_json,
        status=PENDING,
        attempt_count=0,
        next_attempt_at=now,
        created_at=now,
    ))
    db.info[_DIRTY] = True                # wake the worker after this txn commits
    return True


def claim_batch(
    db: Session,
    *,
    limit: int,
    now: float,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> list[MemoryIndexOutbox]:
    """Atomically claim up to ``limit`` due pending rows: flip them to
    in_progress with a lease so a crash mid-batch is recoverable. Ordered by
    ``next_attempt_at`` so retries don't starve fresh work indefinitely.

    The claim is a SINGLE ``UPDATE ... RETURNING`` statement: a separate
    SELECT-then-UPDATE is NOT atomic under SQLite's default isolation, so two
    workers could both read then both claim the same rows. One statement makes
    the read-and-flip atomic — the loser's UPDATE simply matches different (or
    zero) still-PENDING rows."""
    table = MemoryIndexOutbox.__tablename__
    claimed = db.execute(
        text(
            f"UPDATE {table} SET status = :ip, lease_expires_at = :lease "
            f"WHERE id IN ("
            f"  SELECT id FROM {table} "
            f"  WHERE status = :pending AND next_attempt_at <= :now "
            f"  ORDER BY next_attempt_at ASC LIMIT :lim"
            f") RETURNING id"
        ),
        {"ip": IN_PROGRESS, "lease": now + lease_seconds,
         "pending": PENDING, "now": now, "lim": limit},
    ).fetchall()
    db.commit()                          # expires the session → next query reloads fresh state
    ids = [r[0] for r in claimed]
    if not ids:
        return []
    return (
        db.query(MemoryIndexOutbox)
        .filter(MemoryIndexOutbox.id.in_(ids))
        .order_by(MemoryIndexOutbox.next_attempt_at.asc())
        .all()
    )


def mark_done(db: Session, row_id: int, *, now: float) -> None:
    row = db.get(MemoryIndexOutbox, row_id)
    if row is None:
        return
    row.status = DONE
    row.completed_at = now
    row.last_error = None
    row.lease_expires_at = None
    db.commit()


def mark_failed(
    db: Session,
    row_id: int,
    *,
    error: str,
    now: float,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_backoff_s: float = DEFAULT_BASE_BACKOFF_S,
) -> str:
    """Record a failed attempt. Returns the resulting status (pending|dead).

    Exponential backoff on ``next_attempt_at``; after ``max_attempts`` the row
    becomes ``dead`` (dead-letter) — visible in index-status, re-queueable."""
    row = db.get(MemoryIndexOutbox, row_id)
    if row is None:
        return DEAD
    row.attempt_count = (row.attempt_count or 0) + 1
    row.last_error = (error or "")[:2000]
    row.lease_expires_at = None
    if row.attempt_count >= max_attempts:
        row.status = DEAD
    else:
        row.status = PENDING
        # 5s, 10s, 20s, 40s, ... capped at 1h.
        backoff = min(base_backoff_s * (2 ** (row.attempt_count - 1)), 3600.0)
        row.next_attempt_at = now + backoff
    db.commit()
    return row.status


def defer(db: Session, row_id: int, *, delay_s: float, now: float) -> None:
    """Reschedule a claimed row WITHOUT counting a failure. Used when a
    dependency (Qdrant/embedder) is transiently unavailable: a multi-hour
    outage must not exhaust ``max_attempts`` and dead-letter healthy work.
    The row returns to pending; FTS was already updated for degraded search."""
    row = db.get(MemoryIndexOutbox, row_id)
    if row is None:
        return
    row.status = PENDING
    row.lease_expires_at = None
    row.next_attempt_at = now + delay_s
    db.commit()


def reclaim_expired_leases(db: Session, *, now: float) -> int:
    """Restart/crash recovery: in_progress rows whose lease expired revert to
    pending so the worker retries them. Returns count reclaimed."""
    rows = (
        db.query(MemoryIndexOutbox)
        .filter(
            MemoryIndexOutbox.status == IN_PROGRESS,
            MemoryIndexOutbox.lease_expires_at.isnot(None),
            MemoryIndexOutbox.lease_expires_at < now,
        )
        .all()
    )
    for r in rows:
        r.status = PENDING
        r.lease_expires_at = None
    if rows:
        db.commit()
    return len(rows)


def stats(db: Optional[Session] = None) -> dict[str, int]:
    """Counts by status for index-status reporting."""
    own = db is None
    if own:
        db = SessionLocal()
    try:
        from sqlalchemy import func
        rows = (
            db.query(MemoryIndexOutbox.status, func.count(MemoryIndexOutbox.id))
            .group_by(MemoryIndexOutbox.status)
            .all()
        )
        return {status: count for status, count in rows}
    finally:
        if own:
            db.close()
