"""LadybugDB dense + GraphRAG retrieval provider (memory v2).

The dense leg (HNSW ``QUERY_VECTOR_INDEX``) PLUS a one-hop entity-linking boost
(memories that share an entity with the top vector hits — the multi-hop signal),
both owner-scoped. Returns [] when the store or embedder is unavailable so the
orchestrator degrades to FTS-only. The sole dense/graph retrieval backend.
"""
from __future__ import annotations

from services.indexing.embedding_provider import EmbeddingProvider
from services.indexing.ladybug_store import LadybugStore
from services.retrieval.contracts import (
    Evidence,
    EvidenceScores,
    EvidenceSource,
    RetrievalProvider,
    RetrievalRequest,
    evidence_kind,
)


class LadybugProvider(RetrievalProvider):
    def __init__(self, store: LadybugStore, embedding: EmbeddingProvider,
                 max_distance: float | None = None, max_hops: int = 1):
        self.store = store
        self.embedding = embedding
        # Relevance gate: drop dense hits whose cosine distance exceeds this
        # (= 1 - recall_min_similarity). None disables the gate.
        self.max_distance = max_distance
        # GraphRAG expansion depth (memory→memory steps through shared entities);
        # user-configurable via settings.graph_max_hops.
        self.max_hops = max_hops

    def is_available(self) -> bool:
        return self.store is not None and self.embedding.is_available()

    async def search(self, request: RetrievalRequest, *, limit: int) -> list[Evidence]:
        if not self.is_available():
            return []
        qvec = self.embedding.embed_query(request.query)
        hits = self.store.vector_search(
            owner=request.owner_agent_name, query_embedding=qvec, limit=limit,
            max_distance=self.max_distance)
        # One graph hop: memories sharing an entity with the vector hits. The
        # GraphRAG multi-hop signal — pulls in related context a pure-vector
        # search misses (mem0's +23.1 multi-hop class).
        linked = self.store.linked_memories(
            owner=request.owner_agent_name,
            record_ids=[h.record_id for h in hits], limit=limit, max_hops=self.max_hops)

        evidence: list[Evidence] = []
        seen: set[str] = set()
        rank = 0
        for h in list(hits) + list(linked):
            if h.owner != request.owner_agent_name:          # defense in depth
                continue
            if request.types and h.memory_type not in request.types:
                continue
            if h.record_id in seen:
                continue
            seen.add(h.record_id)
            rank += 1
            evidence.append(Evidence(
                evidence_id=f"{evidence_kind(h.memory_type)}:{h.record_id}",
                record_id=h.record_id,
                owner_agent_name=request.owner_agent_name, memory_type=h.memory_type,
                excerpt=h.content,
                source=EvidenceSource("memory_record", h.record_id, h.created_at),
                scores=EvidenceScores(dense_rank=rank),
                authority=h.authority, confidence=h.confidence))
        return evidence
