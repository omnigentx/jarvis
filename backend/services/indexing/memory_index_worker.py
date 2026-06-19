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


def _broadcast_indexed(n: int) -> None:
    """Best-effort SSE hint that ``n`` records were (re)indexed this drain, so the
    Memory page can refresh index-status / the graph live instead of staying
    stale until a manual reload. Global (no agent) — it's a refresh tick, not a
    per-agent event; the worker runs on the main loop so broadcast is in-loop."""
    try:
        from services.activity_stream import activity_stream_manager
        activity_stream_manager.broadcast({
            "event_type": "memory_indexed", "message": f"{n} indexed",
            "data": {"count": n},
        })
    except Exception:  # noqa: BLE001 — never let a UI hint break indexing
        pass


def _load_entities(entities_json):
    """Parse a MemoryRecord.entities_json → list[{name,etype}] (graph linking)."""
    if not entities_json:
        return []
    try:
        import json
        v = json.loads(entities_json)
        return v if isinstance(v, list) else []
    except Exception as exc:  # noqa: BLE001
        logger.debug("[MEMORY] entities_json parse failed (%r…): %s", entities_json[:40], exc)
        return []


class MemoryIndexWorker:
    DEGRADED_DEFER_S = 60.0
    BATCH_LIMIT = 20
    # Idle backstop ONLY: when nothing is pending/in_progress the worker waits
    # for a notify; this just bounds the worst case if a notify were ever lost
    # (it isn't, single-process). NOT a poll — a long idle re-check, not 2s.
    SAFETY_WATCHDOG_S = 300.0

    def __init__(self, *, qdrant_url: str = "http://localhost:6333",
                 embedding_model: str = "BAAI/bge-m3", embedding_revision: str = ""):
        self._qdrant_url = qdrant_url
        self._embedding_model = embedding_model
        self._embedding_revision = embedding_revision
        self._embedding = None
        self._qdrant = None
        self._running = False
        self._wake: "asyncio.Event | None" = None
        self._loop: "asyncio.AbstractEventLoop | None" = None

    # Lazy providers — re-instantiate Qdrant probe each batch so an outage
    # that ends is detected without a restart.
    def _emb(self):
        if self._embedding is None:
            self._embedding = get_shared_embedding_provider(
                self._embedding_model, self._embedding_revision)
        return self._embedding

    def _qd(self):
        # ONE authoritative backend decision (hot-reloadable via settings),
        # SYMMETRIC with the reader (orchestrator). When the configured backend
        # is LadybugDB but it can't be opened, we DEFER the dense write (return a
        # LadybugIndexer with store=None → is_available()==False) — we do NOT
        # silently divert writes to Qdrant, which would land data the reader
        # (FTS-only on the same failure) can't see (writer≠reader SSoT bug).
        try:
            from services.memory.settings import get_memory_settings
            cfg = get_memory_settings()
            backend = getattr(cfg, "vector_backend", "ladybug")
        except Exception:  # noqa: BLE001
            backend, cfg = "ladybug", None
        if backend == "ladybug":
            from services.indexing.ladybug_store import LadybugIndexer, get_ladybug_store
            store = None
            try:
                path = getattr(cfg, "ladybug_path", "data/memory_graph") if cfg else "data/memory_graph"
                store = get_ladybug_store(path)
            except Exception as exc:  # noqa: BLE001
                import logging
                logging.getLogger("memory.index_worker").warning(
                    "[MEMORY] LadybugDB unavailable; deferring dense index "
                    "(FTS still written, will retry): %s", exc)
            return LadybugIndexer(store)
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
            if stats["done"]:
                _broadcast_indexed(stats["done"])     # nudge open Memory panels to re-fetch
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
            # _index_episodic writes a dense node too — prune must remove it
            # symmetrically (same as EVENT_MEMORY_DELETE), else a pruned doc
            # stays permanently dense-searchable (read-after-prune SSoT leak).
            qd = self._qd()
            if qd.is_available():
                qd.delete_by_record(row.aggregate_id)
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
                "relations": _load_entities(rec.relations_json),  # KG triples → RELATES edges
                "embedding_revision": emb.revision(),
                "index_revision": revision,
                "excerpt": chunk,
            },
        } for i, (chunk, vec) in enumerate(zip(chunks, vecs))]
        qd.upsert_points(points)
        db.commit()
        return False

    async def run_loop(self):
        """EVENT-DRIVEN drain (no fixed polling). Wakes on ``ob.notify()`` — fired
        post-commit by any transaction that enqueued an intent — and at the next
        retry/lease deadline. The outbox stays the durable source of truth: the
        first iteration is the startup drain, and a (theoretically) lost notify is
        recovered by the deadline path, so nothing is ever lost.

        Loop: clear the wake flag, drain a batch; if the batch was full there is
        more backlog → drain again immediately; otherwise sleep until a notify or
        the next deadline (whichever first)."""
        self._running = True
        self._wake = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        # Thread-safe wake: notify() runs in a sync commit which may not be on the
        # event-loop thread.
        ob.set_notifier(lambda: self._loop.call_soon_threadsafe(self._wake.set))
        logger.info("[MEMORY] index worker started (event-driven)")
        try:
            while self._running:
                self._wake.clear()                    # clear BEFORE draining → no lost-notify window
                try:
                    stats = await self.process_pending(now=time.time(), limit=self.BATCH_LIMIT)
                except Exception as exc:  # noqa: BLE001
                    logger.error("[MEMORY] index loop error: %s", exc, exc_info=True)
                    stats = {}
                if sum(stats.values()) >= self.BATCH_LIMIT:
                    continue                           # full batch → backlog → keep draining
                try:
                    await asyncio.wait_for(self._wake.wait(),
                                           timeout=self._sleep_delay(now=time.time()))
                except asyncio.TimeoutError:
                    pass                               # deadline/watchdog → re-drain
        finally:
            ob.set_notifier(None)
            logger.info("[MEMORY] index worker stopped")

    def _sleep_delay(self, *, now: float) -> float:
        """Seconds until the worker must wake absent a notify: the nearest
        retry/lease deadline, else the idle safety watchdog."""
        db = SessionLocal()
        try:
            deadline = ob.next_deadline(db, now=now)
        finally:
            db.close()
        if deadline is None:
            return self.SAFETY_WATCHDOG_S
        return max(0.0, deadline - now)

    def stop(self):
        self._running = False
        # Unblock the wait so shutdown is immediate (don't sit on the watchdog).
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)
