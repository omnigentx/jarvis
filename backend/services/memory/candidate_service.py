"""Candidate lifecycle (spec §11). LLMs/compactor/router PROPOSE candidates;
this service deduplicates, routes them (deterministic vs curator vs approval),
and — only on approval — asks MemoryService to persist.

``memory_candidates.status`` is the ONE authoritative candidate state. When a
candidate needs user approval an approval row is created for the unified
inbox, but resolution flows back here (write-through) and the candidate row is
the source every reader trusts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid

from sqlalchemy.orm import Session

from core.database import MemoryCandidate
from services.memory.memory_service import MemoryService
from services.memory.models import CandidateStatus
from services.memory.sensitivity import has_secret

logger = logging.getLogger("memory.candidate")


def _dedupe_key(owner: str, candidate_type: str, content: str) -> str:
    norm = " ".join((content or "").split()).lower()
    return hashlib.sha256(f"{owner}\x1f{candidate_type}\x1f{norm}".encode()).hexdigest()


def create_candidate(
    db: Session, *, owner_agent_name: str, candidate_type: str, payload: dict,
    now: float | None = None, sources: list[dict] | None = None,
    confidence: float = 0.5, requires_curator: bool = False,
    requires_approval: bool = False, pinned_token_budget: int = 1500,
) -> MemoryCandidate:
    """Create (or return existing) a candidate, then route it. Deterministic
    candidates that need neither curator nor approval are persisted
    immediately (auto_approved)."""
    now = time.time() if now is None else now
    content = payload.get("content", "")
    dedupe = _dedupe_key(owner_agent_name, candidate_type, content)

    existing = (
        db.query(MemoryCandidate)
        .filter(MemoryCandidate.dedupe_key == dedupe,
                MemoryCandidate.status.in_([CandidateStatus.PENDING.value,
                                            CandidateStatus.AUTO_APPROVED.value,
                                            CandidateStatus.APPROVED.value]))
        .first()
    )
    if existing is not None:
        return existing

    # Secrets always require explicit approval, never silent persistence.
    if has_secret(content):
        requires_approval = True

    cand = MemoryCandidate(
        id=uuid.uuid4().hex, owner_agent_name=owner_agent_name,
        candidate_type=candidate_type, payload_json=json.dumps(payload, ensure_ascii=False),
        source_refs_json=json.dumps(sources or [], ensure_ascii=False),
        status=CandidateStatus.PENDING.value, confidence=confidence,
        requires_curator=1 if requires_curator else 0,
        requires_approval=1 if requires_approval else 0,
        dedupe_key=dedupe, created_at=now,
    )
    db.add(cand)
    db.commit()
    _emit("memory_candidate_created", cand)

    if requires_curator:
        return cand                                  # await curator decision
    if requires_approval:
        _create_approval_row(cand)
        return cand
    # Deterministic, unambiguous → persist now.
    _persist_from_candidate(db, cand, CandidateStatus.AUTO_APPROVED.value,
                            now=now, pinned_token_budget=pinned_token_budget)
    return cand


def approve_candidate(db: Session, candidate_id: str, *, now: float | None = None,
                      changed_by: str = "user", pinned_token_budget: int = 1500,
                      _from_approval: bool = False) -> MemoryCandidate:
    now = time.time() if now is None else now
    cand = db.get(MemoryCandidate, candidate_id)
    if cand is None:
        raise ValueError("candidate not found")
    if cand.status in (CandidateStatus.APPROVED.value, CandidateStatus.AUTO_APPROVED.value):
        return cand
    _persist_from_candidate(db, cand, CandidateStatus.APPROVED.value,
                            now=now, changed_by=changed_by,
                            pinned_token_budget=pinned_token_budget)
    if not _from_approval:
        _close_linked_approval(candidate_id, "approve")
    return cand


def reject_candidate(db: Session, candidate_id: str, *, now: float | None = None,
                     reason: str | None = None, _from_approval: bool = False) -> MemoryCandidate:
    now = time.time() if now is None else now
    cand = db.get(MemoryCandidate, candidate_id)
    if cand is None:
        raise ValueError("candidate not found")
    cand.status = CandidateStatus.REJECTED.value
    cand.resolved_at = now
    cand.resolution_json = json.dumps({"reason": reason}) if reason else None
    db.commit()
    _emit("memory_candidate_rejected", cand)
    if not _from_approval:
        _close_linked_approval(candidate_id, "reject")
    return cand


def _close_linked_approval(candidate_id: str, decision: str) -> None:
    """Reverse SSoT sync: when a candidate is resolved on the Memory page, close
    its inbox card so the sidebar badge + Approvals list don't show a stale
    pending item. The candidate is the SSoT; this just keeps the mirror honest.
    Best-effort. ``resolve_approval`` re-enters here with ``_from_approval=True``
    on an already-resolved candidate (idempotent), so it terminates."""
    try:
        from services.approval_service import approval_service
        approval_service.resolve_memory_candidate_card(candidate_id, decision)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MEMORY] linked approval close failed: %s", exc)


def ingest_compactor_candidates(
    db: Session, *, owner_agent_name: str, candidates: list[dict],
    now: float | None = None, approval_policy: str = "manual",
    pinned_token_budget: int = 1500,
) -> list[str]:
    """Turn compactor-extracted ``memory_candidates`` (spec §11.2) into
    candidate rows. Compaction stays independent — this is called AFTER the
    compaction result is safely persisted, and any failure here is the
    caller's to swallow. Returns the candidate ids created."""
    now = time.time() if now is None else now
    ids: list[str] = []
    for c in candidates or []:
        content = c.get("content", "")
        if not content:
            continue
        explicit = bool(c.get("explicit"))
        high_conf = c.get("confidence", 0.0) >= 0.9
        # auto only when policy allows AND the compactor was explicit+confident
        requires_approval = not (approval_policy == "auto_low_risk" and explicit and high_conf)
        cand = create_candidate(
            db, owner_agent_name=owner_agent_name,
            candidate_type=f"compaction_{c.get('type', 'semantic')}",
            payload={"memory_type": c.get("type", "semantic"), "content": content,
                     "subject_scope": c.get("subject_scope", f"agent:{owner_agent_name}"),
                     "authority": "agent_observed", "subtype": c.get("subtype")},
            sources=[{"source_type": "compaction", "source_id": str(idx)}
                     for idx in c.get("source_message_indexes", [])],
            confidence=c.get("confidence", 0.5),
            requires_approval=requires_approval, now=now,
            pinned_token_budget=pinned_token_budget,
        )
        ids.append(cand.id)
    return ids


def _persist_from_candidate(db, cand, status, *, now, changed_by="system",
                            pinned_token_budget=1500):
    payload = json.loads(cand.payload_json)
    sources = json.loads(cand.source_refs_json or "[]")

    # ADD-only (memory v2, following mem0's 2026 algorithm): we DON'T resolve
    # conflicts at write time with a curator LLM (lossy supersede + a cost per
    # conflicting write). We just ADD the fact — create_memory already exact-
    # dedups identical content, so literal dups are no-ops while a changed fact
    # (Techcombank→FPT) keeps BOTH, dated. Conflicting versions are resolved at
    # READ time by recency-weighted ranking. Lossless + cheaper.
    svc = MemoryService(db, pinned_token_budget=pinned_token_budget)
    svc.create_memory(
        owner_agent_name=cand.owner_agent_name, memory_type=payload["memory_type"],
        content=payload["content"], subject_scope=payload["subject_scope"],
        authority=payload["authority"], sources=sources, changed_by=changed_by,
        now=now, entities=payload.get("entities"))

    cand.status = status
    cand.resolved_at = now
    db.commit()
    _emit("memory_candidate_approved", cand)


def _create_approval_row(cand: MemoryCandidate) -> None:
    """Mirror onto the unified approvals inbox so the user discovers pending
    memories from the sidebar badge + Approvals page (not only the agent's
    Memory tab). candidate.status stays the SSoT; this card is just a surface.

    ``content`` is REQUIRED by create_approval (it was missing → KeyError →
    the card was silently never created). ``pause=False`` is CRITICAL: the
    default pauses the requesting agent, which for an in-process Jarvis would
    FREEZE the live chat over a memory proposal — these cards just sit in the
    inbox, blocking nothing (same as cron deferred gates)."""
    try:
        from services.approval_service import approval_service
        content = json.loads(cand.payload_json).get("content", "")
        approval_service.create_approval({
            "approval_type": "memory_candidate",
            "agent_name": cand.owner_agent_name,
            "title": f"Remember: {content[:80]}",
            "content": content,
            "pause": False,
            "metadata": {"candidate_id": cand.id},
        })
    except Exception as exc:  # noqa: BLE001 — inbox mirror is best-effort
        logger.warning("[MEMORY] approval row create failed: %s", exc)


def _emit(event_type: str, cand: MemoryCandidate) -> None:
    try:
        from services.activity_stream import activity_stream_manager
        activity_stream_manager.broadcast({
            "agent_name": cand.owner_agent_name, "event_type": event_type,
            "message": event_type, "timestamp": cand.resolved_at or cand.created_at,
            "data": {"candidate_id": cand.id, "candidate_type": cand.candidate_type},
        })
    except Exception:  # noqa: BLE001
        pass
