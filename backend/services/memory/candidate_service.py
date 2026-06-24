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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.database import MemoryCandidate
from services.memory.memory_service import MemoryService
from services.memory.models import CandidateStatus
from services.memory.sensitivity import has_secret

logger = logging.getLogger("memory.candidate")


_OPEN_STATUSES = [CandidateStatus.PENDING.value,
                  CandidateStatus.AUTO_APPROVED.value,
                  CandidateStatus.APPROVED.value]


def _find_open_dup(db: Session, dedupe: str):
    """The open (pending/auto-approved/approved) candidate for this dedupe key,
    if any. Used both for the pre-INSERT read-check and the post-IntegrityError
    fallback so the two agree on what 'already proposed' means."""
    return (db.query(MemoryCandidate)
            .filter(MemoryCandidate.dedupe_key == dedupe,
                    MemoryCandidate.status.in_(_OPEN_STATUSES))
            .first())


def _dedupe_key(owner: str, content: str, subject_scope: str = "user") -> str:
    # Dedup ACROSS lanes: the same fact proposed by the agent's `remember` tool
    # (candidate_type="agent_remember") and the background extractor
    # (candidate_type="extracted") must collapse to ONE card. candidate_type is
    # therefore NOT part of the key — only (owner, subject_scope, normalized
    # content), mirroring the active-record unique index
    # (owner, normalized_content, subject_scope) so candidate dedup and record
    # dedup agree. subject_scope IS keyed: a user-fact and an agent-observation
    # of the same text are genuinely different memories.
    norm = " ".join((content or "").split()).lower()
    return hashlib.sha256(f"{owner}\x1f{subject_scope}\x1f{norm}".encode()).hexdigest()


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
    dedupe = _dedupe_key(owner_agent_name, content, payload.get("subject_scope", "user"))

    existing = _find_open_dup(db, dedupe)
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
    try:
        db.commit()
    except IntegrityError:
        # Concurrency backstop (same shape as create_memory): a sibling lane
        # captured the SAME fact and committed between our read-check (L46) and
        # this commit. The partial UNIQUE index uq_candidate_open_dedup rejects
        # the dup — fall back to the winner instead of two pending cards.
        db.rollback()
        winner = _find_open_dup(db, dedupe)
        if winner is not None:
            return winner
        raise
    _emit("memory_candidate_created", cand)

    if requires_curator:
        return cand                                  # await curator decision
    if requires_approval:
        _create_approval_row(cand)
        # Live chat chip: surface the pending memory IN CONTEXT (with inline
        # approve/reject) instead of leaving it silent until the Approvals tab.
        _emit_saved(cand, status="pending")
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
    _emit_saved(cand, status="rejected")    # live chat chip: pending → dismissed
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


def approval_reason(payload: dict, content: str = "") -> str | None:
    """Why this candidate needs human approval — a STABLE CODE the UI localizes
    (never UI copy here). Lets the inbox explain why a memory still needs review
    even when the user enabled auto-save: ``unverified_evidence`` = the extractor
    couldn't cite verifiable evidence (fail-loud), ``secret`` = sensitive content
    always needs a human. None = no special reason (e.g. the policy is manual)."""
    if content and has_secret(content):
        return "secret"
    if payload.get("excerpt_ok") is False:
        return "unverified_evidence"
    return None


def _confidence_metadata(payload: dict) -> dict:
    """The provenance of a memory's confidence, for MemoryVersion.metadata_json.
    Only the keys the capture lane actually recorded (absent for legacy/manual
    paths) so the blob stays honest about what's known."""
    meta = {}
    for key in ("confidence_method", "reasoning_type", "excerpt_ok"):
        if payload.get(key) is not None:
            meta[key] = payload[key]
    return meta


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
    rec = svc.create_memory(
        owner_agent_name=cand.owner_agent_name, memory_type=payload["memory_type"],
        content=payload["content"], subject_scope=payload["subject_scope"],
        authority=payload["authority"], sources=sources, changed_by=changed_by,
        # SINGLE chokepoint for confidence: every capture path funnels through
        # here, so threading it ONCE fixes all of them. The extracted/fast lane
        # stores the LLM's confidence in the payload; the compaction lane sets
        # the candidate column. Prefer payload, fall back to the column — without
        # this, create_memory defaulted to 0.5 and confidence never reached a
        # memory (the policy's confidence rank-boost was permanently a no-op).
        confidence=payload.get("confidence", cand.confidence),
        # Audit trail: HOW this confidence was derived (method + signals) →
        # MemoryVersion.metadata_json. Lets a future formula change migrate safely.
        version_metadata=_confidence_metadata(payload),
        now=now, entities=payload.get("entities"), subtype=payload.get("subtype"),
        # A SECRET only reaches persistence via EXPLICIT user approval: secrets
        # force requires_approval=True in create_candidate, so they never hit
        # AUTO_APPROVED. Authorize the write only on the human-approved path;
        # auto-approve stays locked so a secret can never persist unattended.
        allow_secret=(status == CandidateStatus.APPROVED.value))

    cand.status = status
    cand.resolved_at = now
    db.commit()
    _emit("memory_candidate_approved", cand)
    # Live chat chip: this candidate is now an ACTIVE memory (covers BOTH the
    # auto-approved path and a human approval) → tell the chat the moment it lands.
    _emit_saved(cand, status="saved", record_id=(rec.id if rec is not None else None))

    # SINGLE-SOURCE graph projection: extract this memory's triples (→ RELATES +
    # MENTIONS) off the hot path, for EVERY lane incl. the agent's free-text
    # `remember`. Deterministic per-memory, not a debounced owner-wide rescan.
    # create_memory exact-dedups (may return an existing record); only graph when
    # triples are still missing, so re-approving a duplicate doesn't re-extract.
    if rec is not None and not rec.relations_json:
        from services.memory.knowledge_graph import schedule_extract_and_store
        schedule_extract_and_store(rec.id)


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
        payload = json.loads(cand.payload_json)
        content = payload.get("content", "")
        # NEVER render a detected secret on the Approvals page. The card is a
        # more-visible surface than memory_records; mask the preview so the
        # cleartext value isn't exposed in the inbox. The real value stays in
        # the candidate payload until a human authorizes persistence (where
        # allow_secret is gated in _persist_from_candidate).
        display = "🔒 hidden secret — approve to store" if has_secret(content) else content
        approval_service.create_approval({
            "approval_type": "memory_candidate",
            "agent_name": cand.owner_agent_name,
            "title": f"Remember: {display[:80]}",
            "content": display,
            "pause": False,
            # ``reason`` (a stable code, localized by the UI) explains why this
            # still needs review even under auto-save — see approval_reason.
            "metadata": {"candidate_id": cand.id,
                         "reason": approval_reason(payload, content)},
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


def _emit_saved(cand: MemoryCandidate, *, status: str, record_id: str | None = None) -> None:
    """Live SSE for the CHAT "memory saved" chip — so the user knows the moment
    Jarvis stores something, in context, instead of discovering it later in the
    Memory tab. Distinct from ``_emit`` (which feeds the Memory tab) so the two
    surfaces stay decoupled, mirroring how recall has its own ``memory_recalled``.

    ``status``: ``saved`` (auto-approved OR human-approved → now active),
    ``pending`` (manual policy / secret / high-risk → awaiting approval), or
    ``rejected``. The chat store keys items by ``candidate_id`` so a later
    transition (pending→saved/rejected) updates the SAME chip in place. Secrets
    are MASKED here too — the cleartext never rides the SSE bus into the chat."""
    try:
        from services.activity_stream import activity_stream_manager
        payload = json.loads(cand.payload_json or "{}")
        raw = payload.get("content", "") or ""
        sensitive = has_secret(raw)
        activity_stream_manager.broadcast({
            "agent_name": cand.owner_agent_name, "event_type": "memory_saved",
            "message": "memory_saved", "timestamp": cand.resolved_at or cand.created_at,
            "data": {
                "candidate_id": cand.id,
                "record_id": record_id,
                "content": "🔒 hidden secret" if sensitive else raw,
                "memory_type": payload.get("memory_type", "semantic"),
                "status": status,
                "sensitive": sensitive,
            },
        })
    except Exception as exc:  # noqa: BLE001 — never break a write over a UI mirror
        logger.debug("[MEMORY] memory_saved SSE emit failed: %s", exc)
