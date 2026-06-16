"""Authorized communication search (spec §14). Re-checks participant access
at QUERY time — an agent may only retrieve a communication it sent or
received. Defense-in-depth on top of the owner-scoped episodic projection.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.database import CommunicationRecord
from services.retrieval.contracts import (
    Evidence,
    EvidenceScores,
    EvidenceSource,
    RetrievalProvider,
    RetrievalRequest,
)

logger = logging.getLogger("memory.comm_provider")

_EXCERPT_CHARS = 400


def _authorized(rec: CommunicationRecord, agent_name: str) -> bool:
    if rec.sender == agent_name:
        return True
    try:
        return agent_name in (json.loads(rec.recipients_json or "[]") or [])
    except (ValueError, TypeError) as exc:
        # Malformed recipients_json → deny (fail-closed), but surface it: a
        # corrupted row would otherwise silently hide a legitimate comm forever.
        logger.warning("[MEMORY] comm %s recipients_json unparseable, denying: %s", rec.id, exc)
        return False


class CommunicationProvider(RetrievalProvider):
    def __init__(self, db: Session):
        self.db = db

    async def search(self, request: RetrievalRequest, *, limit: int) -> list[Evidence]:
        like = f"%{request.query.strip()}%"
        # Coarse candidate fetch by participant; authorization re-checked below.
        rows = (
            self.db.query(CommunicationRecord)
            .filter(or_(
                CommunicationRecord.sender == request.owner_agent_name,
                CommunicationRecord.recipients_json.like(f"%{request.owner_agent_name}%"),
            ))
            .filter(or_(
                CommunicationRecord.subject.like(like),
                CommunicationRecord.body.like(like),
            ))
            .order_by(CommunicationRecord.created_at.desc())
            .limit(limit * 2)
            .all()
        )
        evidence: list[Evidence] = []
        for rank, rec in enumerate(rows, start=1):
            if not _authorized(rec, request.owner_agent_name):
                continue
            body = rec.body or ""
            excerpt = body if len(body) <= _EXCERPT_CHARS else body[:_EXCERPT_CHARS] + " …"
            evidence.append(Evidence(
                evidence_id=f"comm:{rec.id}",
                record_id=rec.id,
                owner_agent_name=request.owner_agent_name,
                memory_type="episodic",
                excerpt=f"{rec.subject or ''}\n{excerpt}".strip(),
                source=EvidenceSource(rec.channel, rec.id, rec.created_at or 0.0),
                scores=EvidenceScores(bm25_rank=rank),
                authority="external_document",
                confidence=0.8,
            ))
            if len(evidence) >= limit:
                break
        return evidence
