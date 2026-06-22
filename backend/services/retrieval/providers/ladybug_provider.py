"""LadybugDB dense + GraphRAG retrieval provider (memory v2).

The dense leg (HNSW ``QUERY_VECTOR_INDEX``) PLUS a query-entity-anchored graph
boost (memories mentioning an entity NAMED IN THE QUERY, ubiquitous hub entities
excluded), both owner-scoped. Returns [] when the store or embedder is unavailable
so the orchestrator degrades to FTS-only. The sole dense/graph retrieval backend.
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
        # GraphRAG, QUERY-ENTITY-ANCHORED (not blind seed co-occurrence): expand
        # only from entities the query actually names, skipping ubiquitous hub
        # entities. This stops the user's own entity — which co-occurs with nearly
        # every personal fact — from dragging tangential memories into unrelated
        # queries (the 2026-06-22 "AI-career memory in a baby-age query" bug).
        # ``graph_max_hops <= 0`` disables the lane entirely (off-switch).
        linked = ([] if self.max_hops <= 0 else self.store.query_anchored_memories(
            owner=request.owner_agent_name, query=request.query, limit=limit))

        # Tag dense vs graph PROVENANCE distinctly so the debug UI can show each
        # lane's contribution. A memory reachable by BOTH (a vector hit that also
        # shares an entity) keeps its dense_rank — the dedup below adds it from the
        # vector list first — so graph_rank marks memories the GRAPH uniquely
        # surfaced (vector-far but entity-linked). Separate rank counters per lane.
        evidence: list[Evidence] = []
        seen: set[str] = set()
        for is_graph, lst in ((False, hits), (True, linked)):
            rank = 0
            for h in lst:
                if h.owner != request.owner_agent_name:      # defense in depth
                    continue
                if request.types and h.memory_type not in request.types:
                    continue
                if h.record_id in seen:
                    continue
                seen.add(h.record_id)
                rank += 1
                scores = (EvidenceScores(graph_rank=rank) if is_graph
                          else EvidenceScores(dense_rank=rank))
                evidence.append(Evidence(
                    evidence_id=f"{evidence_kind(h.memory_type)}:{h.record_id}",
                    record_id=h.record_id,
                    owner_agent_name=request.owner_agent_name, memory_type=h.memory_type,
                    excerpt=h.content,
                    source=EvidenceSource("memory_record", h.record_id, h.created_at),
                    scores=scores,
                    authority=h.authority, confidence=h.confidence))
        return evidence
