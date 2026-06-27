"""Memory settings API — ``/api/memory/settings``.

Typed facade over the generic config DB (category ``memory``): defaults
merged in on read, validation on write, the curator API key write-only and
masked on read. Audit history / export still flow through config_service.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import verify_api_key
from services.activity_stream import activity_stream_manager
from services.memory.settings import get_memory_settings, update_memory_settings

logger = logging.getLogger("memory_settings_api")
router = APIRouter(prefix="/api/memory", tags=["memory"])


def _kick_reranker_warm(model_name: str) -> None:
    """After a rerank-model switch, download + warm the new model in a daemon
    thread and stream progress to the activity SSE — so the UI shows a progress
    bar instead of the FIRST recall silently hanging on the download/load.

    The warm runs off the event loop (it blocks); progress is marshalled back
    onto the loop with ``call_soon_threadsafe`` because ``broadcast`` touches
    asyncio.Queues that are not thread-safe to write from another thread.
    """
    from services.retrieval.reranker import prefetch_and_warm
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    def on_progress(state: str, pct: int) -> None:
        ev = {"event_type": "reranker_model_loading", "state": state,
              "progress": pct, "model": model_name}
        if loop is not None:
            loop.call_soon_threadsafe(activity_stream_manager.broadcast, ev)
        else:
            activity_stream_manager.broadcast(ev)

    threading.Thread(target=prefetch_and_warm, args=(model_name, on_progress),
                     name="reranker-prefetch", daemon=True).start()


class MemorySettingsPatch(BaseModel):
    """All optional — PATCH semantics. Validation lives in
    services.memory.settings.validate_settings_updates (single source)."""

    enabled: bool | None = None
    mode: str | None = None
    auto_capture_preferences: bool | None = None
    extract_every_n: int | None = None
    approval_policy: str | None = None
    pinned_token_budget: int | None = None
    evidence_token_budget: int | None = None
    curator_model: str | None = None
    curator_provider: str | None = None
    curator_base_url: str | None = None
    curator_api_key: str | None = None  # write-only secret
    embedding_model: str | None = None
    embedding_revision: str | None = None
    reranker_enabled: bool | None = None
    rerank_model: str | None = None
    rerank_top_k: int | None = None
    rerank_min_score: float | None = None
    retention_episodic_days: int | None = None
    retention_retrieval_runs_days: int | None = None
    recall_min_similarity: float | None = None
    graph_max_hops: int | None = None
    hub_max_df: float | None = None
    trigger_lexicon_overrides: dict | None = None
    quality_gate_thresholds: dict | None = None


@router.get("/settings", dependencies=[Depends(verify_api_key)])
async def get_settings() -> dict[str, Any]:
    return asdict(get_memory_settings())


@router.patch("/settings", dependencies=[Depends(verify_api_key)])
async def patch_settings(patch: MemorySettingsPatch) -> dict[str, Any]:
    # ``exclude_unset`` so a field omitted by the client is untouched, but an
    # explicit null (e.g. clearing the api key with "") still comes through.
    updates = {k: v for k, v in patch.model_dump(exclude_unset=True).items() if v is not None}
    try:
        settings = update_memory_settings(updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    logger.info("[MEMORY] Settings updated: %s", sorted(updates))
    # A rerank-model switch would otherwise download + load on the first recall
    # (a multi-second silent hang). Pre-warm in the background and stream
    # progress to the UI. Only when the reranker is actually enabled.
    if "rerank_model" in updates and settings.reranker_enabled:
        _kick_reranker_warm(settings.rerank_model)
    return asdict(settings)
