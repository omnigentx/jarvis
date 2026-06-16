"""Memory REST API — agent-scoped reads, manual search, reindex, index status.

Mutating routes (archive/rollback/delete, candidate approve/reject) are added
with MemoryService in the write-pipeline workstream; they must go through the
single write authority, not poke tables here.

Every route filters by the path agent name, so no agent can read another
agent's memory through this surface.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth import verify_api_key
from core.database import (
    MemoryCandidate,
    MemoryRecord,
    MemoryVersion,
    RetrievalRun,
    get_db_session,
)
from services.indexing import consistency_service as cs
from services.memory import candidate_service as cnd
from services.memory.memory_service import MemoryService, MemoryWriteError
from services.memory.settings import get_memory_settings
from services.retrieval.contracts import RetrievalRequest
from services.retrieval.orchestrator import RetrievalOrchestrator

logger = logging.getLogger("routes.memory")
router = APIRouter(prefix="/api", tags=["memory"], dependencies=[Depends(verify_api_key)])


def _memory_dict(rec: MemoryRecord) -> dict:
    return {
        "id": rec.id, "owner_agent_name": rec.owner_agent_name,
        "memory_type": rec.memory_type, "memory_subtype": rec.memory_subtype,
        "subject_scope": rec.subject_scope, "content": rec.content,
        "status": rec.status, "importance": rec.importance, "confidence": rec.confidence,
        "authority": rec.authority, "sensitivity": rec.sensitivity, "pinned": bool(rec.pinned),
        "valid_from": rec.valid_from, "valid_until": rec.valid_until,
        "current_version": rec.current_version,
        "created_at": rec.created_at, "updated_at": rec.updated_at,
    }


@router.get("/agents/{name}/memories")
async def list_memories(
    name: str,
    memory_type: str | None = None,
    status: str = "active",
    limit: int = Query(50, le=200),
    offset: int = 0,
) -> dict[str, Any]:
    db = get_db_session()
    try:
        q = db.query(MemoryRecord).filter(MemoryRecord.owner_agent_name == name)
        if status:
            q = q.filter(MemoryRecord.status == status)
        if memory_type:
            q = q.filter(MemoryRecord.memory_type == memory_type)
        total = q.count()
        rows = q.order_by(MemoryRecord.updated_at.desc()).offset(offset).limit(limit).all()
        return {"total": total, "items": [_memory_dict(r) for r in rows]}
    finally:
        db.close()


@router.get("/agents/{name}/memories/{memory_id}")
async def get_memory(name: str, memory_id: str) -> dict[str, Any]:
    db = get_db_session()
    try:
        rec = db.get(MemoryRecord, memory_id)
        if rec is None or rec.owner_agent_name != name:
            raise HTTPException(status_code=404, detail="memory not found")
        return _memory_dict(rec)
    finally:
        db.close()


@router.get("/agents/{name}/memories/{memory_id}/versions")
async def get_memory_versions(name: str, memory_id: str) -> dict[str, Any]:
    db = get_db_session()
    try:
        rec = db.get(MemoryRecord, memory_id)
        if rec is None or rec.owner_agent_name != name:
            raise HTTPException(status_code=404, detail="memory not found")
        rows = (
            db.query(MemoryVersion).filter(MemoryVersion.memory_id == memory_id)
            .order_by(MemoryVersion.version.desc()).all()
        )
        return {"items": [{
            "version": v.version, "content": v.content, "change_type": v.change_type,
            "changed_by": v.changed_by, "reason": v.reason, "created_at": v.created_at,
        } for v in rows]}
    finally:
        db.close()


@router.get("/agents/{name}/retrieval-runs")
async def list_retrieval_runs(name: str, limit: int = Query(50, le=200)) -> dict[str, Any]:
    db = get_db_session()
    try:
        rows = (
            db.query(RetrievalRun).filter(RetrievalRun.owner_agent_name == name)
            .order_by(RetrievalRun.created_at.desc()).limit(limit).all()
        )
        return {"items": [{
            "id": r.id, "mode": r.mode, "query_hash": r.query_hash,
            "route": r.route_json, "result_ids": r.result_ids_json,
            "total_ms": r.total_ms, "evidence_tokens": r.evidence_tokens,
            "cache_hit": bool(r.cache_hit), "status": r.status, "created_at": r.created_at,
        } for r in rows]}
    finally:
        db.close()


class MemorySearchBody(BaseModel):
    query: str
    types: list[str] | None = None
    mode: str = "balanced"
    limit: int = Field(5, ge=1, le=50)   # bound POST body like the GET routes (le=200)


@router.post("/agents/{name}/memory-search")
async def memory_search(name: str, body: MemorySearchBody) -> dict[str, Any]:
    db = get_db_session()
    try:
        orch = RetrievalOrchestrator(db, get_memory_settings())
        req = RetrievalRequest(owner_agent_name=name, query=body.query,
                               types=body.types or [], mode=body.mode, limit=body.limit)
        # Manual UI search is always an explicit request → run fast retrieval.
        result = await orch.retrieve(req, now=time.time(), agent_requested=True)
        return {
            "level": result.level, "degraded": result.degraded,
            "degraded_reason": result.degraded_reason,
            "evidence": [e.to_dict() for e in result.evidence],
        }
    finally:
        db.close()


class RollbackBody(BaseModel):
    to_version: int


def _svc(db) -> MemoryService:
    return MemoryService(db, pinned_token_budget=get_memory_settings().pinned_token_budget)


@router.post("/agents/{name}/memories/{memory_id}/archive")
async def archive_memory(name: str, memory_id: str) -> dict[str, Any]:
    db = get_db_session()
    try:
        rec = _svc(db).archive_memory(memory_id, owner_agent_name=name)
        return _memory_dict(rec)
    except MemoryWriteError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    finally:
        db.close()


@router.post("/agents/{name}/memories/{memory_id}/rollback")
async def rollback_memory(name: str, memory_id: str, body: RollbackBody) -> dict[str, Any]:
    db = get_db_session()
    try:
        rec = _svc(db).rollback_memory(memory_id, body.to_version, owner_agent_name=name)
        return _memory_dict(rec)
    except MemoryWriteError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        db.close()


@router.delete("/agents/{name}/memories/{memory_id}")
async def delete_memory(name: str, memory_id: str) -> dict[str, Any]:
    db = get_db_session()
    try:
        rec = _svc(db).delete_memory(memory_id, owner_agent_name=name)
        return {"status": "deleted", "id": rec.id}
    except MemoryWriteError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    finally:
        db.close()


@router.get("/agents/{name}/memory-candidates")
async def list_candidates(name: str, status: str = "pending",
                          limit: int = Query(50, le=200)) -> dict[str, Any]:
    db = get_db_session()
    try:
        q = db.query(MemoryCandidate).filter(MemoryCandidate.owner_agent_name == name)
        if status:
            q = q.filter(MemoryCandidate.status == status)
        rows = q.order_by(MemoryCandidate.created_at.desc()).limit(limit).all()
        return {"items": [{
            "id": c.id, "candidate_type": c.candidate_type, "status": c.status,
            "payload": c.payload_json, "confidence": c.confidence,
            "requires_curator": bool(c.requires_curator),
            "requires_approval": bool(c.requires_approval), "created_at": c.created_at,
        } for c in rows]}
    finally:
        db.close()


@router.post("/agents/{name}/memory-candidates/{candidate_id}/approve")
async def approve_candidate(name: str, candidate_id: str) -> dict[str, Any]:
    db = get_db_session()
    try:
        c = db.get(MemoryCandidate, candidate_id)
        if c is None or c.owner_agent_name != name:
            raise HTTPException(status_code=404, detail="candidate not found")
        cnd.approve_candidate(db, candidate_id,
                              pinned_token_budget=get_memory_settings().pinned_token_budget)
        return {"status": "approved", "id": candidate_id}
    except MemoryWriteError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        db.close()


@router.post("/agents/{name}/memory-candidates/{candidate_id}/reject")
async def reject_candidate(name: str, candidate_id: str) -> dict[str, Any]:
    db = get_db_session()
    try:
        c = db.get(MemoryCandidate, candidate_id)
        if c is None or c.owner_agent_name != name:
            raise HTTPException(status_code=404, detail="candidate not found")
        cnd.reject_candidate(db, candidate_id)
        return {"status": "rejected", "id": candidate_id}
    finally:
        db.close()


class BulkCandidateBody(BaseModel):
    ids: list[str] = Field(..., max_length=500)   # cap bulk fan-out per request


@router.post("/agents/{name}/memory-candidates/bulk-approve")
async def bulk_approve_candidates(name: str, body: BulkCandidateBody) -> dict[str, Any]:
    """Approve many candidates in one call (UI "approve all" / multi-select).

    Per-candidate failures are collected, never fatal — one bad candidate
    (e.g. pinned-budget overflow) must not block the rest. Owner-scoped: ids
    not owned by this agent are skipped. ``approve_candidate`` is idempotent,
    so re-approving an already-approved id is a harmless no-op.
    """
    db = get_db_session()
    budget = get_memory_settings().pinned_token_budget
    approved, failed = [], []
    try:
        for cid in body.ids:
            c = db.get(MemoryCandidate, cid)
            if c is None or c.owner_agent_name != name:
                failed.append({"id": cid, "error": "not found"})
                continue
            try:
                cnd.approve_candidate(db, cid, pinned_token_budget=budget)
                approved.append(cid)
            except MemoryWriteError as exc:
                failed.append({"id": cid, "error": str(exc)})
        return {"approved": approved, "failed": failed}
    finally:
        db.close()


@router.post("/agents/{name}/memory-candidates/bulk-reject")
async def bulk_reject_candidates(name: str, body: BulkCandidateBody) -> dict[str, Any]:
    """Reject many candidates in one call (multi-select). Owner-scoped."""
    db = get_db_session()
    rejected, failed = [], []
    try:
        for cid in body.ids:
            c = db.get(MemoryCandidate, cid)
            if c is None or c.owner_agent_name != name:
                failed.append({"id": cid, "error": "not found"})
                continue
            cnd.reject_candidate(db, cid)
            rejected.append(cid)
        return {"rejected": rejected, "failed": failed}
    finally:
        db.close()


@router.post("/memory/reindex")
async def reindex() -> dict[str, Any]:
    db = get_db_session()
    try:
        enqueued = cs.rebuild(db, now=time.time())
        return {"status": "queued", "enqueued": enqueued}
    finally:
        db.close()


@router.get("/memory/index-status")
async def index_status() -> dict[str, Any]:
    db = get_db_session()
    try:
        status = cs.status(db)
    finally:
        db.close()
    # Probe Qdrant so the UI can warn when dense search is unavailable instead
    # of failing silently. Reachability + point count.
    cfg = get_memory_settings()
    qdrant = {"configured_url": cfg.qdrant_url, "reachable": False, "points": None}
    try:
        from services.indexing.qdrant_indexer import COLLECTION, get_qdrant_indexer
        idx = get_qdrant_indexer(cfg.qdrant_url)
        if idx.is_available():
            qdrant["reachable"] = True
            try:
                info = idx._get_client().get_collection(COLLECTION)
                qdrant["points"] = info.points_count
            except Exception:
                qdrant["points"] = 0          # collection not created yet
    except Exception:
        pass
    status["qdrant"] = qdrant
    status["enabled"] = cfg.enabled
    return status


@router.get("/agents/{name}/memory-graph")
async def memory_graph(name: str, limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    """Owner-scoped LadybugDB snapshot for the UI graph view: Memory nodes +
    the Entity nodes they mention + MENTIONS edges. ``available=False`` (empty
    payload) when memory is off, the backend isn't LadybugDB, or the graph can't
    be opened — the UI shows an empty-state instead of erroring."""
    empty = {"memories": [], "entities": [], "edges": [], "available": False}
    cfg = get_memory_settings()
    if not cfg.enabled or getattr(cfg, "vector_backend", "ladybug") != "ladybug":
        return empty
    try:
        from services.indexing.ladybug_store import get_ladybug_store
        store = get_ladybug_store(getattr(cfg, "ladybug_path", "data/memory_graph"))
        data = store.graph_dump(owner=name, limit=limit)
        data["available"] = True
        return data
    except Exception as exc:  # noqa: BLE001 — never 500 a read-only viz endpoint
        logger.warning("[MEMORY] memory-graph dump failed: %s", exc)
        return empty
