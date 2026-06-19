"""Email capture (spec §14). Persist an inter-agent email into
``communication_records`` and project it into the episodic memory of each
authorized participant, so it becomes searchable with provenance.

Wire this at the email tool boundary (after-tool-call hook on the email path)
once the concrete email transport is confirmed; ``capture_email`` is the
single entry point so the hook stays thin.
"""
from __future__ import annotations

import json
import time
import uuid

from sqlalchemy.orm import Session

from core.database import CommunicationRecord
from services.indexing import projector


def capture_email(
    db: Session, *, sender: str, recipients: list[str], subject: str, body: str,
    now: float | None = None, channel: str = "email", source_ref: str | None = None,
) -> CommunicationRecord:
    """Persist the email and project it as an episodic document for the sender
    and every recipient (participants are agent names). Owner-scoped retrieval
    means each agent only ever sees its own copy. Caller commits via this fn."""
    now = time.time() if now is None else now
    rec = CommunicationRecord(
        id=uuid.uuid4().hex, channel=channel, sender=sender,
        recipients_json=json.dumps(recipients, ensure_ascii=False),
        subject=subject, body=body, source_ref=source_ref, created_at=now,
    )
    db.add(rec)
    db.flush()

    content = f"Email from {sender} — {subject or '(no subject)'}\n{body}".strip()
    seen: set[str] = set()
    for participant in [sender, *recipients]:
        if not participant or participant in seen:
            continue
        seen.add(participant)
        projector.project_and_enqueue(
            db, owner_agent_name=participant, document_type="email",
            source_id=f"comm:{rec.id}", content=content, now=now,
            metadata={"channel": channel, "communication_id": rec.id},
        )
    db.commit()
    return rec
