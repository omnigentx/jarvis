"""Project authorized historical data into immutable, hash-verified,
SELF-CONTAINED ``episodic_documents`` rows (spec §4.3, §13).

The projected ``content`` is the durable record served to retrieval — it is
never re-derived from mutable session files. Projection is idempotent via a
content hash so re-running over the same source does not create duplicates.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy.orm import Session

from core.database import EpisodicDocument
from services.indexing import outbox_service as ob


def content_hash(content: str) -> str:
    """Stable hash of normalized content (whitespace-collapsed)."""
    normalized = " ".join((content or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def project_episodic(
    db: Session,
    *,
    owner_agent_name: str,
    document_type: str,
    source_id: str,
    content: str,
    now: float,
    session_id: str | None = None,
    run_id: str | None = None,
    metadata: dict | None = None,
) -> EpisodicDocument | None:
    """Create one episodic document. Returns None if an identical-content doc
    already exists for this owner (dedupe). Does NOT commit — caller owns the
    transaction so projection + outbox enqueue are atomic."""
    content = (content or "").strip()
    if not content:
        return None
    h = content_hash(content)
    existing = (
        db.query(EpisodicDocument.id)
        .filter(
            EpisodicDocument.owner_agent_name == owner_agent_name,
            EpisodicDocument.content_hash == h,
        )
        .first()
    )
    if existing:
        return None
    doc = EpisodicDocument(
        id=uuid.uuid4().hex,
        owner_agent_name=owner_agent_name,
        session_id=session_id,
        run_id=run_id,
        document_type=document_type,
        source_id=source_id,
        content=content,
        metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
        content_hash=h,
        created_at=now,
        indexed_revision=0,
    )
    db.add(doc)
    db.flush()  # assign PK without committing
    return doc


def project_and_enqueue(
    db: Session,
    *,
    owner_agent_name: str,
    document_type: str,
    source_id: str,
    content: str,
    now: float,
    session_id: str | None = None,
    run_id: str | None = None,
    metadata: dict | None = None,
) -> EpisodicDocument | None:
    """Project an episodic document AND enqueue its index intent in the same
    transaction. Episodic docs are immutable so the index revision is always
    1. Caller commits."""
    doc = project_episodic(
        db,
        owner_agent_name=owner_agent_name,
        document_type=document_type,
        source_id=source_id,
        content=content,
        now=now,
        session_id=session_id,
        run_id=run_id,
        metadata=metadata,
    )
    if doc is None:
        return None
    ob.enqueue(
        db,
        event_type=ob.EVENT_EPISODIC_UPSERT,
        aggregate_id=doc.id,
        aggregate_revision=1,
        now=now,
    )
    return doc
