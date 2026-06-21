"""SQLite FTS5 retrieval provider — the degraded / always-available BM25 leg.

Used as the BM25 source when dense vectors are not wired, and as the
sole retrieval path when the dense backend is unreachable. The owner filter is applied in
SQL by ``fts_index.fts_search`` — there is no code path here that can query
across agents.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from core.database import EpisodicDocument, MemoryRecord
from services.indexing import fts_index
from services.retrieval.contracts import (
    Evidence,
    EvidenceScores,
    EvidenceSource,
    RetrievalProvider,
    RetrievalRequest,
)

_EXCERPT_CHARS = 400


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= _EXCERPT_CHARS else text[:_EXCERPT_CHARS].rstrip() + " …"


class SqliteFtsProvider(RetrievalProvider):
    def __init__(self, db: Session):
        self.db = db

    async def search(self, request: RetrievalRequest, *, limit: int) -> list[Evidence]:
        rows = fts_index.fts_search(
            self.db, owner_agent_name=request.owner_agent_name,
            query=request.query, limit=limit,
        )
        evidence: list[Evidence] = []
        for rank, row in enumerate(rows, start=1):
            kind, doc_id, content = row["doc_kind"], row["doc_id"], row["content"]
            if kind == fts_index.KIND_MEMORY:
                rec = self.db.get(MemoryRecord, doc_id)
                if rec is None or rec.status != "active":
                    continue
                if request.types and rec.memory_type not in request.types:
                    continue
                evidence.append(Evidence(
                    evidence_id=f"memory:{doc_id}",
                    record_id=doc_id,
                    owner_agent_name=rec.owner_agent_name,
                    memory_type=rec.memory_type,
                    excerpt=_clip(content),
                    source=EvidenceSource("memory_record", doc_id, rec.created_at or 0.0),
                    scores=EvidenceScores(bm25_rank=rank),
                    authority=rec.authority,
                    confidence=rec.confidence,
                    valid_from=rec.valid_from,
                    valid_until=rec.valid_until,
                ))
            else:  # episodic
                doc = self.db.get(EpisodicDocument, doc_id)
                if doc is None:
                    continue
                if request.types and "episodic" not in request.types:
                    continue
                evidence.append(Evidence(
                    evidence_id=f"episodic:{doc_id}",
                    record_id=doc_id,
                    owner_agent_name=doc.owner_agent_name,
                    memory_type="episodic",
                    excerpt=_clip(content),
                    source=EvidenceSource("session_message", doc.source_id, doc.created_at or 0.0),
                    scores=EvidenceScores(bm25_rank=rank),
                    authority="agent_observed",
                    confidence=0.7,
                ))
        return evidence
