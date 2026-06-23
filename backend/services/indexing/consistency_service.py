"""Rebuild + status + retention for the memory index.

SQLite is the source of truth; this module re-derives the LadybugDB/FTS index
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
    (re)indexing. Returns the number of intents enqueued. The worker drains
    them into a fresh index.

    ``force=True``: a rebuild MUST re-process records whose outbox row is
    already ``done`` (the common case — they were indexed under the previous
    backend). Without it ``enqueue`` skips them on the UNIQUE constraint and
    the rebuild silently enqueues 0 (the bug that left pre-LadybugDB memories
    permanently absent from the graph → recall fell back to FTS-only)."""
    count = 0
    for (rid, rev) in db.query(MemoryRecord.id, MemoryRecord.current_version).filter(
        MemoryRecord.status == "active"
    ):
        if ob.enqueue(db, event_type=ob.EVENT_MEMORY_UPSERT, aggregate_id=rid,
                      aggregate_revision=rev, now=now, force=True):
            count += 1
    for (did,) in db.query(EpisodicDocument.id):
        if ob.enqueue(db, event_type=ob.EVENT_EPISODIC_UPSERT, aggregate_id=did,
                      aggregate_revision=1, now=now, force=True):
            count += 1
    db.commit()
    logger.info("[MEMORY] rebuild enqueued %d index intents", count)
    return count


_MEMORY_CATEGORY = "memory"
_EMBED_REV_KEY = "indexed_embedding_revision"


def migrate_on_embedding_change(db: Session, cfg, *, now: float) -> dict:
    """If the configured embedding model/revision differs from what the graph was
    last projected with, WIPE the graph + re-project from SQLite so the HNSW index
    is rebuilt over the NEW vectors (the index is static — in-place re-embed leaves
    stale vectors). Returns what happened (for logging/tests).

    GUARD (review #1): probe that the NEW model's backend is importable BEFORE
    wiping. Otherwise a deploy that ships this code but hasn't installed the new
    dep would wipe a HEALTHY graph and then fail to re-embed (Null provider) →
    dense recall dead with no rollback. If the dep is missing we skip the wipe and
    log loud — degrade in place, never self-inflict the very total-recall failure
    this subsystem guards against."""
    from services.config_service import config_service
    cur_rev = (getattr(cfg, "embedding_revision", "") or getattr(cfg, "embedding_model", "") or "")
    if not cur_rev:
        return {"migrated": False, "reason": "no_model"}
    prev_rev = config_service.get(_MEMORY_CATEGORY, _EMBED_REV_KEY)
    if prev_rev == cur_rev:
        return {"migrated": False, "reason": "unchanged"}

    from services.indexing.embedding_provider import get_embedding_provider
    if not get_embedding_provider(getattr(cfg, "embedding_model", ""),
                                  getattr(cfg, "embedding_revision", "")).is_available():
        logger.error("[MEMORY] embedding model changed (%r → %r) but its backend is "
                     "NOT installed — SKIPPING the graph wipe (run the 'memory' extra "
                     "/ uv sync). Keeping the existing graph rather than wiping it into "
                     "a dead state.", prev_rev, cur_rev)
        return {"migrated": False, "reason": "deps_missing"}

    from services.indexing.ladybug_store import reset_ladybug_store
    reset_ladybug_store(getattr(cfg, "ladybug_path", "data/memory_graph"))
    enq = rebuild(db, now=now)
    # Mark AFTER enqueue: outbox intents persist across restarts, so a crash
    # mid-drain just re-drains; the marker won't wrongly skip a half-done migration.
    config_service.set(_MEMORY_CATEGORY, _EMBED_REV_KEY, cur_rev)
    logger.info("[MEMORY] embedding model changed (%r → %r) → wiped graph + "
                "re-embedding %d records with the new model", prev_rev, cur_rev, enq)
    return {"migrated": True, "enqueued": enq}


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
    index projection (FTS/LadybugDB) is removed too. Returns rows removed."""
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
