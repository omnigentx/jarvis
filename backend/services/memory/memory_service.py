"""MemoryService — the ONLY write authority for durable memory (spec §11.5).

LLMs propose; this service validates, versions, authorizes, and persists.
Every write: derives/validates the owner, validates scope+authority, scans for
secrets, dedupes, enforces the pinned-token budget, creates an immutable
version + provenance, enqueues an index intent in the SAME transaction, and
emits an SSE lifecycle event. It must never trust an LLM-supplied owner/scope.
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.database import MemoryRecord, MemorySource, MemoryVersion
from services.indexing import outbox_service as ob
from services.indexing.chunker import estimate_tokens
from services.memory.models import (
    PIN_FORBIDDEN_AUTHORITIES,
    Authority,
    MemoryStatus,
    Sensitivity,
    validate_subject_scope,
)
from services.memory.sensitivity import classify_sensitivity

logger = logging.getLogger("memory.service")


class MemoryWriteError(Exception):
    """Raised when a write is rejected (bad scope, secret, pinned over budget)."""


def _normalize(content: str) -> str:
    return " ".join((content or "").split()).lower()


def _emit(event_type: str, owner: str, now: float, data: dict) -> None:
    """Best-effort SSE — never breaks a write."""
    try:
        from services.activity_stream import activity_stream_manager
        activity_stream_manager.broadcast({
            "agent_name": owner, "event_type": event_type,
            "message": data.get("message", event_type), "timestamp": now, "data": data,
        })
    except Exception as exc:  # noqa: BLE001
        logger.debug("[MEMORY] sse emit failed: %s", exc)


class MemoryService:
    def __init__(self, db: Session, *, pinned_token_budget: int = 1500):
        self.db = db
        self.pinned_token_budget = pinned_token_budget

    # ── create ──────────────────────────────────────────────────────────
    def create_memory(
        self, *, owner_agent_name: str, memory_type: str, content: str,
        subject_scope: str, authority: str, now: float | None = None,
        subtype: str | None = None, importance: float = 0.5, confidence: float = 0.5,
        pinned: bool = False, sources: list[dict] | None = None,
        changed_by: str = "system", reason: str | None = None,
        allow_secret: bool = False, entities: list | None = None,
    ) -> MemoryRecord:
        now = time.time() if now is None else now
        if not owner_agent_name:
            raise MemoryWriteError("owner_agent_name is required (trusted, never LLM-supplied)")
        validate_subject_scope(subject_scope)            # raises on free-form scope
        if authority not in {a.value for a in Authority}:
            raise MemoryWriteError(f"unknown authority {authority!r}")

        sensitivity = classify_sensitivity(content)
        if sensitivity == Sensitivity.SECRET.value and not allow_secret:
            raise MemoryWriteError("content contains a secret; refusing to persist")
        if pinned and authority in PIN_FORBIDDEN_AUTHORITIES:
            raise MemoryWriteError(f"authority {authority!r} cannot be pinned")
        if pinned and sensitivity != Sensitivity.NORMAL.value:
            raise MemoryWriteError("sensitive content cannot be auto-pinned")

        # Exact-dedup key = owner + normalized_content + subject_scope (spec §7).
        # NOT memory_type — the same fact extracted once as a preference→semantic
        # and once as an instruction→pinned must still collapse to one memory.
        existing = (
            self.db.query(MemoryRecord)
            .filter(MemoryRecord.owner_agent_name == owner_agent_name,
                    MemoryRecord.normalized_content == _normalize(content),
                    MemoryRecord.subject_scope == subject_scope,
                    MemoryRecord.status == MemoryStatus.ACTIVE.value)
            .first()
        )
        if existing is not None:
            return existing                              # exact duplicate → no-op

        if pinned:
            self._assert_pinned_budget(owner_agent_name, estimate_tokens(content))

        rec = MemoryRecord(
            id=uuid.uuid4().hex, owner_agent_name=owner_agent_name,
            memory_type=memory_type, memory_subtype=subtype,
            subject_scope=subject_scope, content=content,
            normalized_content=_normalize(content), status=MemoryStatus.ACTIVE.value,
            importance=importance, confidence=confidence, authority=authority,
            sensitivity=sensitivity, pinned=1 if pinned else 0,
            current_version=1, created_at=now, updated_at=now,
            entities_json=json.dumps(entities) if entities else None,
        )
        self.db.add(rec)
        self.db.flush()
        self._add_version(rec, content, "create", changed_by, reason, now)
        self._add_sources(rec, sources, now)
        ob.enqueue(self.db, event_type=ob.EVENT_MEMORY_UPSERT, aggregate_id=rec.id,
                   aggregate_revision=rec.current_version, now=now)
        try:
            self.db.commit()
        except IntegrityError:
            # Concurrency backstop: a sibling capture of the SAME fact won the
            # race and committed between our read-check and this commit. The
            # partial unique index (uq_memory_active_dedup) rejects the dup —
            # fall back to the winner instead of surfacing a 500/silent loss.
            self.db.rollback()
            winner = (
                self.db.query(MemoryRecord)
                .filter(MemoryRecord.owner_agent_name == owner_agent_name,
                        MemoryRecord.normalized_content == _normalize(content),
                        MemoryRecord.subject_scope == subject_scope,
                        MemoryRecord.status == MemoryStatus.ACTIVE.value)
                .first()
            )
            if winner is not None:
                return winner
            raise
        _emit("memory_created", owner_agent_name, now,
              {"memory_id": rec.id, "memory_type": memory_type, "pinned": pinned})
        return rec

    # ── update / supersede ──────────────────────────────────────────────
    def update_content(self, memory_id: str, new_content: str, *,
                       owner_agent_name: str, now: float | None = None,
                       changed_by: str = "user", reason: str | None = None) -> MemoryRecord:
        now = time.time() if now is None else now
        rec = self._owned(memory_id, owner_agent_name)
        if classify_sensitivity(new_content) == Sensitivity.SECRET.value:
            raise MemoryWriteError("content contains a secret; refusing to persist")
        rec.content = new_content
        rec.normalized_content = _normalize(new_content)
        rec.current_version += 1
        rec.updated_at = now
        self._add_version(rec, new_content, "update", changed_by, reason, now)
        ob.enqueue(self.db, event_type=ob.EVENT_MEMORY_UPSERT, aggregate_id=rec.id,
                   aggregate_revision=rec.current_version, now=now)
        self.db.commit()
        _emit("memory_updated", owner_agent_name, now, {"memory_id": rec.id})
        return rec

    def archive_memory(self, memory_id: str, *, owner_agent_name: str,
                       now: float | None = None, changed_by: str = "user") -> MemoryRecord:
        return self._set_status(memory_id, MemoryStatus.ARCHIVED.value, "memory_archived",
                                owner_agent_name, changed_by, now)

    def restore_memory(self, memory_id: str, *, owner_agent_name: str,
                       now: float | None = None, changed_by: str = "user") -> MemoryRecord:
        """Reverse of archive: bring a memory back to ACTIVE. Unlike the other
        status changes this enqueues an UPSERT (not DELETE) so the worker
        re-projects it into the search index + graph."""
        now = time.time() if now is None else now
        rec = self._owned(memory_id, owner_agent_name)
        rec.status = MemoryStatus.ACTIVE.value
        rec.current_version += 1
        rec.updated_at = now
        self._add_version(rec, rec.content, "memory_restored", changed_by, None, now)
        ob.enqueue(self.db, event_type=ob.EVENT_MEMORY_UPSERT, aggregate_id=rec.id,
                   aggregate_revision=rec.current_version, now=now)
        self.db.commit()
        _emit("memory_restored", owner_agent_name, now, {"memory_id": rec.id})
        return rec

    def supersede_memory(self, memory_id: str, *, owner_agent_name: str,
                         now: float | None = None, changed_by: str = "curator") -> MemoryRecord:
        """Mark a memory superseded by a newer one (conflict resolution). The
        record + its version history stay; only the status changes, so the old
        truth is preserved and auditable."""
        return self._set_status(memory_id, MemoryStatus.SUPERSEDED.value, "memory_superseded",
                                owner_agent_name, changed_by, now)

    def delete_memory(self, memory_id: str, *, owner_agent_name: str,
                      now: float | None = None, changed_by: str = "user") -> MemoryRecord:
        return self._set_status(memory_id, MemoryStatus.DELETED.value, "memory_deleted",
                                owner_agent_name, changed_by, now)

    def rollback_memory(self, memory_id: str, to_version: int, *, owner_agent_name: str,
                        now: float | None = None, changed_by: str = "user") -> MemoryRecord:
        now = time.time() if now is None else now
        rec = self._owned(memory_id, owner_agent_name)
        ver = (self.db.query(MemoryVersion)
               .filter(MemoryVersion.memory_id == memory_id,
                       MemoryVersion.version == to_version).first())
        if ver is None:
            raise MemoryWriteError(f"version {to_version} not found")
        rec.content = ver.content
        rec.normalized_content = _normalize(ver.content)
        rec.current_version += 1
        rec.status = MemoryStatus.ACTIVE.value
        rec.updated_at = now
        self._add_version(rec, ver.content, "rollback", changed_by,
                          f"rollback to v{to_version}", now)
        ob.enqueue(self.db, event_type=ob.EVENT_MEMORY_UPSERT, aggregate_id=rec.id,
                   aggregate_revision=rec.current_version, now=now)
        self.db.commit()
        _emit("memory_updated", owner_agent_name, now,
              {"memory_id": rec.id, "rolled_back_to": to_version})
        return rec

    # ── helpers ─────────────────────────────────────────────────────────
    def _owned(self, memory_id: str, owner: str) -> MemoryRecord:
        rec = self.db.get(MemoryRecord, memory_id)
        if rec is None or rec.owner_agent_name != owner:
            raise MemoryWriteError("memory not found for this owner")
        return rec

    def _set_status(self, memory_id, status, event, owner, changed_by, now) -> MemoryRecord:
        now = time.time() if now is None else now
        rec = self._owned(memory_id, owner)
        rec.status = status
        rec.current_version += 1                      # status change is a new version
        rec.updated_at = now
        self._add_version(rec, rec.content, status, changed_by, None, now)
        # Removal from the search index via the outbox.
        ob.enqueue(self.db, event_type=ob.EVENT_MEMORY_DELETE, aggregate_id=rec.id,
                   aggregate_revision=rec.current_version, now=now)
        self.db.commit()
        _emit(event, owner, now, {"memory_id": rec.id})
        return rec

    def _add_version(self, rec, content, change_type, changed_by, reason, now) -> None:
        self.db.add(MemoryVersion(
            memory_id=rec.id, version=rec.current_version, content=content,
            metadata_json="{}", change_type=change_type, changed_by=changed_by,
            reason=reason, created_at=now))

    def _add_sources(self, rec, sources, now) -> None:
        for s in (sources or []):
            self.db.add(MemorySource(
                memory_id=rec.id, memory_version=rec.current_version,
                source_type=s.get("source_type", "unknown"),
                source_id=s.get("source_id", ""),
                source_agent_name=s.get("source_agent_name"),
                source_excerpt=s.get("source_excerpt"),
                source_hash=s.get("source_hash"),
                source_timestamp=s.get("source_timestamp"),
                authority=s.get("authority", rec.authority), created_at=now))

    def _assert_pinned_budget(self, owner: str, incoming_tokens: int) -> None:
        rows = (self.db.query(MemoryRecord.content)
                .filter(MemoryRecord.owner_agent_name == owner,
                        MemoryRecord.pinned == 1,
                        MemoryRecord.status == MemoryStatus.ACTIVE.value).all())
        used = sum(estimate_tokens(c) for (c,) in rows)
        if used + incoming_tokens > self.pinned_token_budget:
            raise MemoryWriteError(
                f"pinned token budget exceeded ({used + incoming_tokens} > "
                f"{self.pinned_token_budget}); unpin something first")
