"""Qdrant dense retrieval provider (the dense leg of hybrid search).

Guarded: returns [] when Qdrant or the embedder is unavailable so the
orchestrator degrades to FTS-only. The owner filter is MANDATORY and built
inside this provider — there is no code path that queries without it.
"""
from __future__ import annotations

from services.indexing.embedding_provider import EmbeddingProvider
from services.indexing.qdrant_indexer import COLLECTION, DENSE_VECTOR, QdrantIndexer
from services.retrieval.contracts import (
    Evidence,
    EvidenceScores,
    EvidenceSource,
    RetrievalProvider,
    RetrievalRequest,
    evidence_kind,
)


class QdrantProvider(RetrievalProvider):
    def __init__(self, indexer: QdrantIndexer, embedding: EmbeddingProvider):
        self.indexer = indexer
        self.embedding = embedding

    def is_available(self) -> bool:
        return self.indexer.is_available() and self.embedding.is_available()

    async def search(self, request: RetrievalRequest, *, limit: int) -> list[Evidence]:
        if not self.is_available():
            return []
        from qdrant_client import models  # lazy

        qvec = self.embedding.embed_query(request.query)
        must = [
            models.FieldCondition(key="owner_agent_name",
                                  match=models.MatchValue(value=request.owner_agent_name)),
            models.FieldCondition(key="status", match=models.MatchValue(value="active")),
        ]
        if request.types:
            must.append(models.FieldCondition(
                key="memory_type", match=models.MatchAny(any=list(request.types))))
        client = self.indexer._get_client()
        hits = client.query_points(
            collection_name=COLLECTION, query=qvec, using=DENSE_VECTOR,
            limit=limit, query_filter=models.Filter(must=must), with_payload=True,
        ).points
        evidence: list[Evidence] = []
        for rank, h in enumerate(hits, start=1):
            p = h.payload or {}
            # Defense in depth: never trust a point whose owner doesn't match.
            if p.get("owner_agent_name") != request.owner_agent_name:
                continue
            _rid = str(p.get("record_id", ""))
            evidence.append(Evidence(
                evidence_id=f"{evidence_kind(p.get('memory_type', 'semantic'))}:{_rid}",
                record_id=_rid,
                owner_agent_name=request.owner_agent_name,
                memory_type=p.get("memory_type", "semantic"),
                excerpt=p.get("excerpt", ""),
                source=EvidenceSource(p.get("source_type", "memory_record"),
                                      str(p.get("source_id", "")), p.get("created_at", 0.0)),
                scores=EvidenceScores(dense_rank=rank),
                authority=p.get("authority", "agent_observed"),
                confidence=float(p.get("confidence", 0.7)),
            ))
        return evidence
