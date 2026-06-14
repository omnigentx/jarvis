"""Rebuild + status + retention for the memory index.

SQLite is the source of truth; this module re-derives the Qdrant/FTS index
from it (acceptance criterion 6) and prunes unbounded-growth tables.
"""
from __future__ import annotations

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.database import (
    EpisodicDocument,
    MemoryRecord,
    MemorySource,
    RetrievalRun,
)
from services.indexing import outbox_service as ob

logger = logging.getLogger("memory.consistency")


def rebuild(db: Session, *, now: float) -> int:
    """Re-enqueue every active memory record and every episodic document for
    (re)indexing. Idempotent at the outbox level. Returns the number of intents
    enqueued. The worker drains them into a fresh index."""
    count = 0
    for (rid, rev) in db.query(MemoryRecord.id, MemoryRecord.current_version).filter(
        MemoryRecord.status == "active"
    ):
        if ob.enqueue(db, event_type=ob.EVENT_MEMORY_UPSERT, aggregate_id=rid,
                      aggregate_revision=rev, now=now):
            count += 1
    for (did,) in db.query(EpisodicDocument.id):
        if ob.enqueue(db, event_type=ob.EVENT_EPISODIC_UPSERT, aggregate_id=did,
                      aggregate_revision=1, now=now):
            count += 1
    db.commit()
    logger.info("[MEMORY] rebuild enqueued %d index intents", count)
    return count


def status(db: Session) -> dict:
    """Counts for the index-status route: SQLite truth vs outbox backlog."""
    mem_by_status = dict(
        db.query(MemoryRecord.status, func.count(MemoryRecord.id))
        .group_by(MemoryRecord.status).all()
    )
    episodic_total = db.query(func.count(EpisodicDocument.id)).scalar() or 0
    episodic_unindexed = (
        db.query(func.count(EpisodicDocument.id))
        .filter(EpisodicDocument.indexed_revision == 0).scalar() or 0
    )
    return {
        "memory_records": mem_by_status,
        "episodic_documents": int(episodic_total),
        "episodic_unindexed": int(episodic_unindexed),
        "outbox": ob.stats(db),
    }


def prune_retrieval_runs(db: Session, *, older_than: float) -> int:
    """Delete retrieval telemetry older than the cutoff. Returns rows removed."""
    n = (
        db.query(RetrievalRun)
        .filter(RetrievalRun.created_at < older_than)
        .delete(synchronize_session=False)
    )
    db.commit()
    return n


def prune_episodic(db: Session, *, older_than: float, now: float) -> int:
    """Delete episodic documents older than the cutoff UNLESS still referenced
    by an active memory's provenance. Enqueues an ``episodic_prune`` so the
    index projection (FTS/Qdrant) is removed too. Returns rows removed."""
    referenced = {
        sid for (sid,) in db.query(MemorySource.source_id).distinct()
    }
    stale = (
        db.query(EpisodicDocument)
        .filter(EpisodicDocument.created_at < older_than)
        .all()
    )
    removed = 0
    for doc in stale:
        if doc.source_id in referenced:
            continue
        ob.enqueue(db, event_type=ob.EVENT_EPISODIC_PRUNE, aggregate_id=doc.id,
                   aggregate_revision=1, now=now)
        db.delete(doc)
        removed += 1
    db.commit()
    return removed
