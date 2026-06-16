"""Typed contracts crossing the retrieval boundary — no raw dicts between
modules. See docs/agent-memory-adaptive-rag-spec.md §7.4, §9, §15.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RetrievalMode(str, Enum):
    ECONOMICAL = "economical"
    BALANCED = "balanced"
    DEEP = "deep"


def evidence_kind(memory_type: str) -> str:
    """Canonical ``evidence_id`` prefix. Every provider MUST agree so the id
    is stable across dense/FTS/graph, and ``memory_fetch`` can route on it:
    episodic documents vs durable memory records map to ``episodic`` /
    ``memory``. The id is then ``f"{evidence_kind(t)}:{record_id}"``.
    """
    return "episodic" if memory_type == "episodic" else "memory"


@dataclass
class EvidenceSource:
    type: str
    id: str
    timestamp: float = 0.0
    uri: Optional[str] = None


@dataclass
class EvidenceScores:
    bm25_rank: Optional[int] = None
    dense_rank: Optional[int] = None
    rrf: Optional[float] = None
    reranker: Optional[float] = None
    final: float = 0.0


@dataclass
class Evidence:
    """The common structure every retriever returns (spec §9)."""
    evidence_id: str
    record_id: str
    owner_agent_name: str
    memory_type: str
    excerpt: str
    source: EvidenceSource
    scores: EvidenceScores
    authority: str
    confidence: float
    valid_from: Optional[float] = None
    valid_until: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "record_id": self.record_id,
            "owner_agent_name": self.owner_agent_name,
            "memory_type": self.memory_type,
            "excerpt": self.excerpt,
            "source": {
                "type": self.source.type,
                "id": self.source.id,
                "timestamp": self.source.timestamp,
                "uri": self.source.uri,
            },
            "scores": {
                "bm25_rank": self.scores.bm25_rank,
                "dense_rank": self.scores.dense_rank,
                "rrf": self.scores.rrf,
                "reranker": self.scores.reranker,
                "final": self.scores.final,
            },
            "authority": self.authority,
            "confidence": self.confidence,
            "validity": {"valid_from": self.valid_from, "valid_until": self.valid_until},
        }


@dataclass
class RetrievalBudget:
    """Hard ceilings (spec §7.4). Constructed only from settings; enforced by
    the orchestrator, never trusted to callers."""
    mode: str = RetrievalMode.BALANCED.value
    max_fast_retrievals: int = 2
    max_agentic_rounds: int = 1
    max_subqueries: int = 3
    max_candidates_per_retriever: int = 30
    max_fused_candidates: int = 20
    max_evidence_items: int = 5
    max_evidence_tokens: int = 2500
    retrieval_timeout_ms: int = 3000
    deep_retrieval_timeout_ms: int = 15000
    planner: str = "on_low_confidence"   # off | on_low_confidence | always
    reranker: str = "on_ambiguity"       # off | on_ambiguity | always


# Mode presets (spec §7.4). Settings may override individual fields; this is
# the single source for the per-mode shape.
DEFAULT_BUDGETS: dict[str, RetrievalBudget] = {
    RetrievalMode.ECONOMICAL.value: RetrievalBudget(
        mode="economical", max_evidence_tokens=1000,
        planner="off", reranker="off", max_agentic_rounds=0,
    ),
    RetrievalMode.BALANCED.value: RetrievalBudget(
        mode="balanced", max_evidence_tokens=2500,
        planner="on_low_confidence", reranker="on_ambiguity", max_agentic_rounds=1,
    ),
    RetrievalMode.DEEP.value: RetrievalBudget(
        mode="deep", max_evidence_tokens=5000,
        planner="on_low_confidence", reranker="always", max_agentic_rounds=2,
    ),
}


@dataclass
class RetrievalRequest:
    """A retrieval ask. ``owner_agent_name`` is set by the backend from the
    trusted tool binding — NEVER from tool arguments."""
    owner_agent_name: str
    query: str
    types: list[str] = field(default_factory=list)
    mode: str = RetrievalMode.BALANCED.value
    limit: int = 5
    subject_scope: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None


@dataclass
class RetrievalResult:
    evidence: list[Evidence] = field(default_factory=list)
    level: int = 0                       # 0 none | 1 fast | 2 agentic
    degraded: bool = False
    degraded_reason: Optional[str] = None
    cache_hit: bool = False
    total_ms: int = 0


@dataclass
class CandidatePayload:
    """A proposed memory (router/compactor/curator → MemoryService).
    ``owner_agent_name`` here is advisory; MemoryService derives the true
    owner from trusted state and ignores/validates this."""
    candidate_type: str
    content: str
    memory_type: str
    subject_scope: str
    confidence: float = 0.5
    explicit: bool = False
    source_refs: list[dict] = field(default_factory=list)
    owner_agent_name: Optional[str] = None
    subtype: Optional[str] = None


class RetrievalProvider(ABC):
    """A pluggable retrieval backend (Qdrant, SQLite FTS, communications)."""

    @abstractmethod
    async def search(self, request: RetrievalRequest, *, limit: int) -> list[Evidence]:
        ...


class GraphProvider(ABC):
    """Interface ONLY — GraphRAG is deferred (no implementation, no callers
    now). Kept so a future graph backend slots in without touching the memory
    domain model, agent tool contracts, or evidence format."""

    @abstractmethod
    async def neighbors(self, record_id: str, *, hops: int) -> list[Evidence]:
        ...
