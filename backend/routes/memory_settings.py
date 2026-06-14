"""Memory settings API — ``/api/memory/settings``.

Typed facade over the generic config DB (category ``memory``): defaults
merged in on read, validation on write, the curator API key write-only and
masked on read. Audit history / export still flow through config_service.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import verify_api_key
from services.memory.settings import get_memory_settings, update_memory_settings

logger = logging.getLogger("memory_settings_api")
router = APIRouter(prefix="/api/memory", tags=["memory"])


class MemorySettingsPatch(BaseModel):
    """All optional — PATCH semantics. Validation lives in
    services.memory.settings.validate_settings_updates (single source)."""

    enabled: bool | None = None
    mode: str | None = None
    auto_capture_preferences: bool | None = None
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
    qdrant_url: str | None = None
    retention_episodic_days: int | None = None
    retention_retrieval_runs_days: int | None = None
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
    return asdict(settings)
