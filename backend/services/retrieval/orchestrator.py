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
import time
import uuid

from sqlalchemy.orm import Session

from core.database import RetrievalRun
from services.indexing.embedding_provider import get_shared_embedding_provider
from services.retrieval import fusion
from services.retrieval.budget import build_budget
from services.retrieval.cache import (
    RetrievalCache, cache_key, normalize_query, settings_fingerprint)
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
        # Dense/graph leg: LadybugDB (HNSW vectors + property graph). Degrades to
        # FTS-only when the store can't be opened — never breaks retrieval.
        from services.indexing.ladybug_store import get_ladybug_store
        from services.retrieval.providers.ladybug_provider import LadybugProvider
        try:
            store = get_ladybug_store(settings.ladybug_path)
        except Exception as exc:  # noqa: BLE001 — degrade to FTS-only, never break retrieval
            logger.warning("[MEMORY] LadybugDB unavailable, FTS-only: %s", exc)
            store = None
        # Relevance gate: cosine distance = 1 - similarity (LadybugDB cosine
        # metric, measured 2026-06-17). An off-topic query whose nearest memory
        # is beyond this distance contributes nothing → no injection.
        min_sim = getattr(settings, "recall_min_similarity", 0.44)
        max_hops = int(getattr(settings, "graph_max_hops", 1) or 1)
        hub_max_df = float(getattr(settings, "hub_max_df", 0.5) or 0.5)
        self._dense = LadybugProvider(store, emb, max_distance=1.0 - float(min_sim),
                                      max_hops=max_hops, hub_max_df=hub_max_df)
        # Cross-encoder reranker (precision stage) — re-scores the fused candidates
        # by reading (query, memory) jointly. None when disabled/unavailable →
        # recall keeps fusion order (never breaks). Shared singleton (model loaded once).
        self._reranker = None
        if getattr(settings, "reranker_enabled", False):
            from services.retrieval.reranker import get_shared_reranker
            self._reranker = get_shared_reranker(
                getattr(settings, "rerank_model", None) or "BAAI/bge-reranker-v2-m3")

    async def retrieve(self, request: RetrievalRequest, *, now: float,
                       ledger: EvidenceLedger | None = None, turn: int = 0,
                       agent_requested: bool = False,
                       continuing_tool_loop: bool = False) -> RetrievalResult:
        # Wall-clock start for telemetry latency. perf_counter (monotonic) — NOT
        # ``now`` (epoch, used for recency boost): the two measure different things.
        t0 = time.perf_counter()
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
                        filters=json.dumps(sorted(request.types)), index_revision=index_rev,
                        settings_fp=settings_fingerprint(self.settings))
        cached = _CACHE.get(key)
        if cached is not None:
            # Copy the cached list: _finalize re-applies the recency/authority
            # policy at the CURRENT ``now`` (so a hot read-only query doesn't
            # freeze recency buckets at first-call time), and that reorder must
            # not mutate the shared cached list under another concurrent reader.
            # Cache hit: lanes/rerank never ran, so their timings stay None — only
            # total_ms (≈ the cache lookup) is meaningful here.
            return self._finalize(request, list(cached), level=LEVEL_FAST, cache_hit=True,
                                  budget=budget, ledger=ledger, turn=turn, now=now, t0=t0)

        fused, dense_failed, lane_ms = await self._fast_round(
            request, budget, bm25_first=decision.bm25_first)
        # Recency/authority/freshness ranking on the HAPPY path too — this is the
        # read-side of ADD-only (a newer fact, e.g. "works at NovaCorp", outranks the
        # superseded "AcmeCorp" for "where do I work now"). Previously this
        # only ran on escalation, so the recency promise never fired on the
        # common balanced-mode path.
        fusion.apply_policy(fused, now=now)
        level = LEVEL_FAST

        weak = is_weak(fused, thresholds=self.settings.quality_gate_thresholds)
        if should_escalate(weak=weak, mode=request.mode,
                           deep_requested=(request.mode == "deep"), high_risk=False,
                           rounds_used=0, max_rounds=budget.max_agentic_rounds):
            extra = await self._corrective_round(request, budget)
            fused = fusion.rrf_fuse([fused, extra]) if extra else fused
            fusion.apply_policy(fused, now=now)
            level = LEVEL_AGENTIC

        # Precision stage: cross-encoder rerank of the fused candidates. Sets
        # scores.reranker (apply_policy then orders by it) and drops off-topic /
        # low-relevance candidates below the floor — what the bi-encoder lanes
        # can't do (the 2026-06-22 "cat memory in a baby-age query" case).
        _rr0 = time.perf_counter()
        fused, reranked = self._apply_rerank(request, fused)
        # Record latency ONLY when the cross-encoder actually scored candidates.
        # A GATED skip (disabled / cold-warming / nothing to rerank) spent ~0ms but
        # didn't run — leave rerank_ms None so the UI renders "—", not a misleading
        # "0ms" that reads as "rerank ran instantly".
        rerank_ms = int((time.perf_counter() - _rr0) * 1000) if reranked else None

        _CACHE.set(key, fused)
        return self._finalize(request, fused, level=level, cache_hit=False,
                              budget=budget, ledger=ledger, turn=turn, now=now,
                              dense_failed=dense_failed, t0=t0,
                              timings={**lane_ms, "rerank_ms": rerank_ms})

    def _apply_rerank(self, request: RetrievalRequest, fused: list[Evidence]
                      ) -> tuple[list[Evidence], bool]:
        """Cross-encoder rerank of the top fused candidates (precision stage).
        Re-scores into ``scores.reranker``, drops those below ``rerank_min_score``,
        returns them ordered by reranker. No-op when disabled/unavailable → fusion
        order kept. Best-effort: a rerank error must never break recall.

        Returns ``(result, ran)``: ``ran`` is True only when the model actually
        scored candidates (so the caller records real latency); a GATED skip
        (nothing to rerank / disabled / cold-warming) returns ``(fused, False)`` so
        telemetry shows "—" rather than a misleading 0ms.

        INTENTIONAL GATE (review #2): the floor can legitimately return [] when every
        candidate scores below it — i.e. the cross-encoder judged them all off-topic
        (measured: an off-topic query reranks the whole set to ~0). This is the
        desired off-topic→nothing behavior; it is NOT the silent-veto bug the dense
        off-topic gate had, because the reranker scored each candidate against THIS
        query (it didn't drop a high-precision lane on a sibling lane's emptiness).
        Keep ``rerank_min_score`` low (0.005) so a weak-but-relevant hit survives."""
        if not fused or self._reranker is None or not self._reranker.is_available():
            return fused, False
        # NEVER block a recall turn on the reranker's cold load. The 0.6B causal
        # LM's first load is slow (~tens of s on CPU); if it isn't warm yet, kick
        # off a one-shot background load and keep fusion order for THIS turn. The
        # lifespan eager-warms it at boot, so this skip window is normally empty —
        # it only catches a turn that races the warm right after startup.
        if hasattr(self._reranker, "is_loaded") and not self._reranker.is_loaded():
            self._reranker.warm_async()
            return fused, False
        top_k = int(getattr(self.settings, "rerank_top_k", 20) or 20)
        floor = float(getattr(self.settings, "rerank_min_score", 0.0) or 0.0)
        # Tail beyond top_k is intentionally DROPPED from recall (not just from
        # reranking): for personal-memory recall we inject far fewer than top_k, and
        # the tail is the lowest-fusion-ranked anyway (review #4).
        cand = fused[:top_k]
        try:
            scores = self._reranker.rerank(request.query, [c.excerpt for c in cand])
        except Exception as exc:  # noqa: BLE001 — keep fusion order on rerank failure
            logger.warning("[MEMORY] rerank failed, keeping fusion order: %s", exc)
            # The model DID run (and consumed wall-clock) — ran=True so a slow
            # failing rerank still surfaces its latency in telemetry.
            return fused, True
        kept = []
        for c, s in zip(cand, scores):
            c.scores.reranker = s
            if s >= floor:
                kept.append(c)
        kept.sort(key=lambda e: e.scores.reranker or 0.0, reverse=True)
        return kept, True

    async def _fast_round(self, request: RetrievalRequest, budget, *,
                          bm25_first: bool = False
                          ) -> tuple[list[Evidence], bool, dict[str, int | None]]:
        cap = budget.max_candidates_per_retriever
        dense_on = self._dense.is_available()

        async def _timed(coro):
            # Per-lane wall-clock for telemetry. Catch HERE (not via gather's
            # return_exceptions) so a lane's latency is recorded even when it
            # raises — the result may be an Exception, the timing still counts.
            _s = time.perf_counter()
            try:
                r = await coro
            except Exception as exc:  # noqa: BLE001 — preserved in results, handled below
                r = exc
            return r, int((time.perf_counter() - _s) * 1000)

        timed = [_timed(self._fts.search(request, limit=cap))]
        if dense_on:
            timed.append(_timed(self._dense.search(request, limit=cap)))
        gathered = await asyncio.gather(*timed)
        results = [g[0] for g in gathered]
        lane_ms: dict[str, int | None] = {
            "bm25_ms": gathered[0][1],
            "dense_ms": gathered[1][1] if dense_on else None,
        }
        lists = [r for r in results if isinstance(r, list)]
        # If the dense lane (tasks[1], only added when dense_on) THREW, the
        # search is degraded even though is_available() said yes — surface it
        # so the caller doesn't report a silent "0 memories, not degraded".
        dense_failed = False
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning("[MEMORY] provider error: %s", r)
                if dense_on and i == 1:
                    dense_failed = True
        # Relevance gate (agentic "retrieve-or-not"): the dense lane is the best
        # semantic judge — when it's healthy yet returns NOTHING within the
        # similarity threshold, the query is off-topic, so the FTS lane's hits are
        # just incidental keyword matches (function words hitting a memory). Drop
        # them → no memory is injected for an unrelated turn.
        #
        # Two deliberate carve-outs:
        #   - bm25_first: an exact-identifier query (email/ID/code) is legitimately
        #     far from stored memories in embedding space, so dense returns [] even
        #     on-topic. Keep the FTS hit — dropping it would lose exact-identifier
        #     recall (the whole point of the router flag).
        #   - dense DOWN (dense_failed / not dense_on): no semantic judge available,
        #     so we CANNOT make the off-topic guarantee — fall back to FTS-only
        #     (degraded, surfaced via the degraded flag). The FTS lane already drops
        #     stopwords/function-words (fts_index._safe_match_expr) so an off-topic
        #     function-word-only query still yields nothing even here.
        if (dense_on and not dense_failed and not bm25_first
                and isinstance(results[1], list) and not results[1]):
            return [], dense_failed, lane_ms
        fused = fusion.rrf_fuse(lists)
        return fused[: budget.max_fused_candidates], dense_failed, lane_ms

    async def _corrective_round(self, request: RetrievalRequest, budget) -> list[Evidence]:
        """Bounded corrective pass: bring in authorized communications. (An LLM
        subquery planner can be injected here later without changing callers.)"""
        try:
            return await self._comm.search(request, limit=budget.max_candidates_per_retriever)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MEMORY] corrective round error: %s", exc)
            return []

    def _finalize(self, request, fused, *, level, cache_hit, budget, ledger, turn, now,
                  t0: float, dense_failed=False,
                  timings: dict[str, int | None] | None = None) -> RetrievalResult:
        # Recency/authority policy is applied HERE (not only pre-cache) so cache
        # hits get fresh recency at the current ``now``. apply_policy is
        # idempotent (re-derives order from rrf + boost(now), no cumulative
        # mutation), so the extra call on the miss path is a harmless no-op.
        fusion.apply_policy(fused, now=now)
        if ledger is not None:
            fused = ledger.dedup(fused, turn=turn)
        selected, tokens = build_evidence(fused, budget)
        if ledger is not None:
            for ev in selected:
                ledger.add(ev, turn=turn)
        # Degraded = dense lane not serving. Two distinct causes: it's offline
        # (is_available() False) OR it errored mid-search (dense_failed) — the
        # latter previously read as a healthy empty result.
        if dense_failed:
            degraded, reason = True, "ladybug_error"
        elif not self._dense.is_available():
            degraded, reason = True, "ladybug_unavailable"
        elif self._dense_unpopulated(fused):
            degraded, reason = True, "ladybug_unpopulated"
        else:
            degraded, reason = False, None
        # total_ms spans the WHOLE call (incl. this finalize stage). Lane timings
        # (bm25/dense/rerank) stay None on the cache-hit path — those stages never
        # ran — which the UI renders as "—" rather than a misleading 0.
        timings = timings or {}
        result = RetrievalResult(
            evidence=selected, level=level, degraded=degraded,
            degraded_reason=reason,
            cache_hit=cache_hit,
            total_ms=int((time.perf_counter() - t0) * 1000),
            bm25_ms=timings.get("bm25_ms"),
            dense_ms=timings.get("dense_ms"),
            rerank_ms=timings.get("rerank_ms"),
        )
        self._write_telemetry(request, result, tokens, now)
        self._emit_completed(request, result, now)
        return result

    def _emit_completed(self, request, result, now) -> None:
        """Best-effort SSE so the chat UI can show a 'memory used' chip and
        clear/raise the degraded banner. Never breaks retrieval.

        Emit on ANY non-degraded turn (even 0 evidence): the frontend clears the
        degraded banner on ``retrieval_completed``, so a recovered-but-empty turn
        must still send it — otherwise a banner raised by an earlier degraded
        turn sticks forever (nit)."""
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

    def _dense_unpopulated(self, fused) -> bool:
        """True when the dense lane is 'available' yet contributed NOTHING and
        its graph is empty — i.e. the index was never (re)projected, not a
        genuine 'no semantic match'. Reported as degraded so the UI/caller knows
        recall is FTS-only (the silent-failure gap from 2026-06-16: an empty
        graph read as a healthy result). Cost-bounded: the ``count()`` graph
        query only runs when dense returned zero ranks (rare once populated)."""
        if any(getattr(e.scores, "dense_rank", None) is not None for e in fused):
            return False                      # dense did contribute → populated
        store = getattr(self._dense, "store", None)
        if store is None:
            return False                      # no store attribute (degraded/FTS-only)
        try:
            return store.count() == 0
        except Exception:  # noqa: BLE001 — never let the check break retrieval
            return False

    def _index_revision(self) -> int:
        # Coarse revision token for cache invalidation. Must change on ANY
        # content OR status change — counting rows alone is NOT enough: an
        # archive/update keeps the row but must invalidate the cache. Summing
        # ``current_version`` covers create (v1), update/archive/delete
        # (version++); episodic count covers new immutable docs.
        #
        # ALSO fold in the latest outbox completion: a (re)projection into the
        # dense/graph index (e.g. the Qdrant→LadybugDB backfill) changes WHAT
        # dense search returns without touching any SQLite version, so without
        # this the cache would keep serving the pre-backfill (dense-less) result
        # for an identical query (the stale-recall bug observed 2026-06-16).
        from core.database import EpisodicDocument, MemoryIndexOutbox, MemoryRecord
        from sqlalchemy import func
        e = self.db.query(func.count(EpisodicDocument.id)).scalar() or 0
        ver = self.db.query(func.coalesce(func.sum(MemoryRecord.current_version), 0)).scalar() or 0
        oc = self.db.query(func.coalesce(func.max(MemoryIndexOutbox.completed_at), 0.0)).scalar() or 0.0
        return int(e) + int(ver) + int(oc * 1000)

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
                bm25_ms=result.bm25_ms,
                dense_ms=result.dense_ms,
                rerank_ms=result.rerank_ms,
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
