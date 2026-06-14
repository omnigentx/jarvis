"""Memory index worker — drains the outbox into FTS5 (always) and Qdrant
(when available). Runs its OWN lightweight asyncio loop (the existing
BackgroundJobScheduler is idle-gated, which is wrong for continuous indexing —
see docs/memory-impl/03). The durable queue is the outbox, so restart recovery
is just reclaiming expired leases.

Degraded modes (spec §20): FTS is updated immediately for degraded search;
the dense/Qdrant part is DEFERRED (not failed) when Qdrant/embeddings are
unavailable, so a long outage never dead-letters healthy work and recovery
drains automatically.
"""
from __future__ import annotations

import asyncio
import logging
import time

from core.database import EpisodicDocument, MemoryRecord, SessionLocal
from services.indexing import chunker, fts_index
from services.indexing import outbox_service as ob
from services.indexing import qdrant_indexer as qi
from services.indexing.embedding_provider import get_shared_embedding_provider

logger = logging.getLogger("memory.index_worker")


def _load_entities(entities_json):
    """Parse a MemoryRecord.entities_json → list[{name,etype}] (graph linking)."""
    if not entities_json:
        return []
    try:
        import json
        v = json.loads(entities_json)
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


class MemoryIndexWorker:
    DEGRADED_DEFER_S = 60.0

    def __init__(self, *, qdrant_url: str = "http://localhost:6333",
                 embedding_model: str = "BAAI/bge-m3", embedding_revision: str = ""):
        self._qdrant_url = qdrant_url
        self._embedding_model = embedding_model
        self._embedding_revision = embedding_revision
        self._embedding = None
        self._qdrant = None
        self._running = False

    # Lazy providers — re-instantiate Qdrant probe each batch so an outage
    # that ends is detected without a restart.
    def _emb(self):
        if self._embedding is None:
            self._embedding = get_shared_embedding_provider(
                self._embedding_model, self._embedding_revision)
        return self._embedding

    def _qd(self):
        # Backend chosen at runtime (hot-reloadable via settings): LadybugDB
        # (embedded graph + HNSW, v2) or legacy Qdrant. Both expose the same
        # is_available/ensure_collection/upsert_points/delete_by_record surface.
        try:
            from services.memory.settings import get_memory_settings
            cfg = get_memory_settings()
            if getattr(cfg, "vector_backend", "ladybug") == "ladybug":
                from services.indexing.ladybug_store import LadybugIndexer, get_ladybug_store
                return LadybugIndexer(
                    get_ladybug_store(getattr(cfg, "ladybug_path", "data/memory_graph")))
        except Exception as exc:  # noqa: BLE001 — fall back to Qdrant on any issue
            import logging
            logging.getLogger("memory.index_worker").warning(
                "[MEMORY] ladybug indexer unavailable, using qdrant: %s", exc)
        return qi.get_qdrant_indexer(self._qdrant_url, dim=self._emb().dim())

    async def process_pending(self, *, now: float, limit: int = 20) -> dict:
        """Drain up to ``limit`` due rows. Returns per-status counts. This is
        the directly-callable unit (no loop) used by tests."""
        db = SessionLocal()
        stats = {"done": 0, "deferred": 0, "failed": 0}
        try:
            ob.reclaim_expired_leases(db, now=now)
            rows = ob.claim_batch(db, limit=limit, now=now)
            for row in rows:
                try:
                    deferred = self._handle(db, row, now)
                    if deferred:
                        ob.defer(db, row.id, delay_s=self.DEGRADED_DEFER_S, now=now)
                        stats["deferred"] += 1
                    else:
                        ob.mark_done(db, row.id, now=now)
                        stats["done"] += 1
                except Exception as exc:  # noqa: BLE001 — record + backoff, never crash the loop
                    logger.error("[MEMORY] index task %s failed: %s", row.id, exc, exc_info=True)
                    ob.mark_failed(db, row.id, error=str(exc), now=now)
                    stats["failed"] += 1
            return stats
        finally:
            db.close()

    # returns True if the dense part was deferred (FTS still updated)
    def _handle(self, db, row, now: float) -> bool:
        et = row.event_type
        if et == ob.EVENT_EPISODIC_UPSERT:
            return self._index_episodic(db, row.aggregate_id, row.aggregate_revision, now)
        if et == ob.EVENT_MEMORY_UPSERT:
            return self._index_memory(db, row.aggregate_id, row.aggregate_revision, now)
        if et == ob.EVENT_EPISODIC_PRUNE:
            fts_index.fts_delete(db, doc_kind=fts_index.KIND_EPISODIC, doc_id=row.aggregate_id)
            db.commit()
            return False
        if et == ob.EVENT_MEMORY_DELETE:
            fts_index.fts_delete(db, doc_kind=fts_index.KIND_MEMORY, doc_id=row.aggregate_id)
            db.commit()
            qd = self._qd()
            if qd.is_available():
                qd.delete_by_record(row.aggregate_id)
            return False
        if et == ob.EVENT_REBUILD_ALL:
            return False  # fan-out is performed by consistency_service.rebuild()
        logger.warning("[MEMORY] unknown outbox event_type %r", et)
        return False

    def _index_episodic(self, db, doc_id, revision, now) -> bool:
        doc = db.get(EpisodicDocument, doc_id)
        if doc is None:
            return False  # deleted before indexing — nothing to do
        fts_index.fts_upsert(db, doc_kind=fts_index.KIND_EPISODIC, doc_id=doc.id,
                             owner_agent_name=doc.owner_agent_name, content=doc.content)
        db.commit()
        qd = self._qd()
        emb = self._emb()
        if not (qd.is_available() and emb.is_available()):
            return True  # defer dense part; FTS already serves degraded search
        qd.ensure_collection()
        chunks = chunker.chunk_document(doc.document_type, doc.content)
        vecs = emb.embed_documents(chunks)
        points = [{
            "id": qi.point_id(doc.id, i, revision),
            "dense": vec,
            "payload": {
                "chunk_id": qi.point_id(doc.id, i, revision),
                "record_id": doc.id,
                "owner_agent_name": doc.owner_agent_name,
                "memory_type": "episodic",
                "source_type": doc.document_type,
                "source_id": doc.source_id,
                "status": "active",
                "created_at": doc.created_at,
                "content_hash": doc.content_hash,
                "embedding_revision": emb.revision(),
                "index_revision": revision,
                "excerpt": chunk,
            },
        } for i, (chunk, vec) in enumerate(zip(chunks, vecs))]
        qd.upsert_points(points)
        doc.indexed_revision = revision
        db.commit()
        return False

    def _index_memory(self, db, record_id, revision, now) -> bool:
        rec = db.get(MemoryRecord, record_id)
        if rec is None or rec.status != "active":
            # archived/deleted memory: ensure it is not searchable.
            fts_index.fts_delete(db, doc_kind=fts_index.KIND_MEMORY, doc_id=record_id)
            db.commit()
            qd = self._qd()
            if qd.is_available():
                qd.delete_by_record(record_id)
            return False
        fts_index.fts_upsert(db, doc_kind=fts_index.KIND_MEMORY, doc_id=rec.id,
                             owner_agent_name=rec.owner_agent_name,
                             content=rec.normalized_content)
        db.commit()
        qd = self._qd()
        emb = self._emb()
        if not (qd.is_available() and emb.is_available()):
            return True
        qd.ensure_collection()
        chunks = chunker.chunk_document(chunker.DOC_FACT, rec.normalized_content)
        vecs = emb.embed_documents(chunks)
        points = [{
            "id": qi.point_id(rec.id, i, revision),
            "dense": vec,
            "payload": {
                "chunk_id": qi.point_id(rec.id, i, revision),
                "record_id": rec.id,
                "owner_agent_name": rec.owner_agent_name,
                "memory_type": rec.memory_type,
                "subject_scope": rec.subject_scope,
                "source_type": "memory_record",
                "status": rec.status,
                "authority": rec.authority,
                "confidence": rec.confidence,
                "created_at": rec.created_at,
                "valid_from": rec.valid_from or rec.created_at,
                "entities": _load_entities(rec.entities_json),   # graph entity linking
                "embedding_revision": emb.revision(),
                "index_revision": revision,
                "excerpt": chunk,
            },
        } for i, (chunk, vec) in enumerate(zip(chunks, vecs))]
        qd.upsert_points(points)
        db.commit()
        return False

    async def run_loop(self, *, interval_s: float = 2.0):
        """Continuous low-priority drain. Started in the FastAPI lifespan when
        memory is enabled."""
        self._running = True
        logger.info("[MEMORY] index worker loop started")
        while self._running:
            try:
                await self.process_pending(now=time.time())
            except Exception as exc:  # noqa: BLE001
                logger.error("[MEMORY] index loop error: %s", exc, exc_info=True)
            await asyncio.sleep(interval_s)

    def stop(self):
        self._running = False
