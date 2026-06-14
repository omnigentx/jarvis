"""Retrieval orchestrator — ties the router, providers, fusion, budget, cache,
ledger, and telemetry together (spec §5 read path, §7, §8).

Level 0 returns immediately (no providers touched). Level 1 runs the available
providers in parallel, fuses with RRF + bounded policy, and trims to budget.
Level 2 is a bounded corrective round (deterministic here; an LLM planner can
be injected later via ``planner`` without changing this control flow). Every
retrieval writes a ``retrieval_runs`` telemetry row.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid

from sqlalchemy.orm import Session

from core.database import RetrievalRun
from services.indexing.embedding_provider import get_shared_embedding_provider
from services.indexing.qdrant_indexer import get_qdrant_indexer
from services.retrieval import fusion
from services.retrieval.budget import build_budget
from services.retrieval.cache import RetrievalCache, cache_key, normalize_query
from services.retrieval.contracts import Evidence, RetrievalRequest, RetrievalResult
from services.retrieval.evidence_builder import build_evidence
from services.retrieval.intent_router import (
    LEVEL_AGENTIC,
    LEVEL_FAST,
    LEVEL_NONE,
    decide_initial,
    should_escalate,
)
from services.retrieval.ledger import EvidenceLedger
from services.retrieval.providers.communication_provider import CommunicationProvider
from services.retrieval.providers.qdrant_provider import QdrantProvider
from services.retrieval.providers.sqlite_fts_provider import SqliteFtsProvider
from services.retrieval.quality_gate import is_weak

logger = logging.getLogger("memory.retrieval")

# Process-wide cache (per-agent keys; never shared across agents by key design).
_CACHE = RetrievalCache()


class RetrievalOrchestrator:
    def __init__(self, db: Session, settings):
        self.db = db
        self.settings = settings
        self._fts = SqliteFtsProvider(db)
        self._comm = CommunicationProvider(db)
        emb = get_shared_embedding_provider(settings.embedding_model, settings.embedding_revision)
        # Dense/graph leg: LadybugDB (v2) or legacy Qdrant, chosen by settings.
        if getattr(settings, "vector_backend", "ladybug") == "ladybug":
            from services.indexing.ladybug_store import get_ladybug_store
            from services.retrieval.providers.ladybug_provider import LadybugProvider
            try:
                store = get_ladybug_store(getattr(settings, "ladybug_path", "data/memory_graph"))
            except Exception as exc:  # noqa: BLE001 — degrade to FTS-only, never break retrieval
                logger.warning("[MEMORY] LadybugDB unavailable, FTS-only: %s", exc)
                store = None
            self._dense = LadybugProvider(store, emb)
        else:
            self._dense = QdrantProvider(get_qdrant_indexer(settings.qdrant_url), emb)

    async def retrieve(self, request: RetrievalRequest, *, now: float,
                       ledger: EvidenceLedger | None = None, turn: int = 0,
                       agent_requested: bool = False,
                       continuing_tool_loop: bool = False) -> RetrievalResult:
        budget = build_budget(request.mode,
                              evidence_token_budget=self.settings.evidence_token_budget)

        decision = decide_initial(
            request.query,
            agent_requested=agent_requested,
            continuing_tool_loop=continuing_tool_loop,
            ledger_has_sufficient=False,
            lexicon_overrides=self.settings.trigger_lexicon_overrides,
        )
        if decision.level == LEVEL_NONE:
            return RetrievalResult(level=LEVEL_NONE)

        if decision.targets:
            request.types = list(decision.targets)

        # Cache (per agent + query + revision).
        index_rev = self._index_revision()
        key = cache_key(owner_agent_name=request.owner_agent_name,
                        normalized_query=normalize_query(request.query),
                        filters=json.dumps(sorted(request.types)), index_revision=index_rev)
        cached = _CACHE.get(key)
        if cached is not None:
            return self._finalize(request, cached, level=LEVEL_FAST, cache_hit=True,
                                  budget=budget, ledger=ledger, turn=turn, now=now)

        fused = await self._fast_round(request, budget)
        level = LEVEL_FAST

        weak = is_weak(fused, thresholds=self.settings.quality_gate_thresholds)
        if should_escalate(weak=weak, mode=request.mode,
                           deep_requested=(request.mode == "deep"), high_risk=False,
                           rounds_used=0, max_rounds=budget.max_agentic_rounds):
            extra = await self._corrective_round(request, budget)
            fused = fusion.rrf_fuse([fused, extra]) if extra else fused
            fusion.apply_policy(fused, now=now)
            level = LEVEL_AGENTIC

        _CACHE.set(key, fused)
        return self._finalize(request, fused, level=level, cache_hit=False,
                              budget=budget, ledger=ledger, turn=turn, now=now)

    async def _fast_round(self, request: RetrievalRequest, budget) -> list[Evidence]:
        cap = budget.max_candidates_per_retriever
        tasks = [self._fts.search(request, limit=cap)]
        if self._dense.is_available():
            tasks.append(self._dense.search(request, limit=cap))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        lists = [r for r in results if isinstance(r, list)]
        for r in results:
            if isinstance(r, Exception):
                logger.warning("[MEMORY] provider error: %s", r)
        fused = fusion.rrf_fuse(lists)
        return fused[: budget.max_fused_candidates]

    async def _corrective_round(self, request: RetrievalRequest, budget) -> list[Evidence]:
        """Bounded corrective pass: bring in authorized communications. (An LLM
        subquery planner can be injected here later without changing callers.)"""
        try:
            return await self._comm.search(request, limit=budget.max_candidates_per_retriever)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MEMORY] corrective round error: %s", exc)
            return []

    def _finalize(self, request, fused, *, level, cache_hit, budget, ledger, turn, now) -> RetrievalResult:
        if ledger is not None:
            fused = ledger.dedup(fused, turn=turn)
        selected, tokens = build_evidence(fused, budget)
        if ledger is not None:
            for ev in selected:
                ledger.add(ev, turn=turn)
        degraded = not self._dense.is_available()
        result = RetrievalResult(
            evidence=selected, level=level, degraded=degraded,
            degraded_reason="qdrant_unavailable" if degraded else None,
            cache_hit=cache_hit, total_ms=0,
        )
        self._write_telemetry(request, result, tokens, now)
        self._emit_completed(request, result, now)
        return result

    def _emit_completed(self, request, result, now) -> None:
        """Best-effort SSE so the chat UI can show a 'memory used' chip and
        clear/raise the degraded banner. Never breaks retrieval."""
        if not result.evidence and not result.degraded:
            return
        try:
            from services.activity_stream import activity_stream_manager
            event_type = "retrieval_degraded" if result.degraded else "retrieval_completed"
            activity_stream_manager.broadcast({
                "agent_name": request.owner_agent_name, "event_type": event_type,
                "message": f"{len(result.evidence)} memories recalled", "timestamp": now,
                "data": {"count": len(result.evidence), "level": result.level,
                         "evidence": [{"id": e.evidence_id, "type": e.memory_type,
                                       "excerpt": e.excerpt[:160]} for e in result.evidence]},
            })
        except Exception:  # noqa: BLE001
            pass

    def _index_revision(self) -> int:
        # Coarse revision token for cache invalidation. Must change on ANY
        # content OR status change — counting rows alone is NOT enough: an
        # archive/update keeps the row but must invalidate the cache. Summing
        # ``current_version`` covers create (v1), update/archive/delete
        # (version++); episodic count covers new immutable docs.
        from core.database import EpisodicDocument, MemoryRecord
        from sqlalchemy import func
        e = self.db.query(func.count(EpisodicDocument.id)).scalar() or 0
        ver = self.db.query(func.coalesce(func.sum(MemoryRecord.current_version), 0)).scalar() or 0
        return int(e) + int(ver)

    def _write_telemetry(self, request, result, tokens, now) -> None:
        try:
            qh = hashlib.sha256(normalize_query(request.query).encode()).hexdigest()[:32]
            self.db.add(RetrievalRun(
                id=uuid.uuid4().hex,
                owner_agent_name=request.owner_agent_name,
                session_id=request.session_id,
                run_id=request.run_id,
                query_hash=qh,
                mode=request.mode,
                route_json=json.dumps({"level": result.level, "degraded": result.degraded}),
                filters_json=json.dumps(sorted(request.types)),
                result_ids_json=json.dumps([e.record_id for e in result.evidence]),
                total_ms=result.total_ms,
                evidence_tokens=tokens,
                cache_hit=1 if result.cache_hit else 0,
                status="ok",
                created_at=now,
            ))
            self.db.commit()
        except Exception as exc:  # noqa: BLE001 — telemetry must never break retrieval
            logger.debug("[MEMORY] telemetry write failed: %s", exc)
            self.db.rollback()
