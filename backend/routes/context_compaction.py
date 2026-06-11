"""Context-compaction settings API — ``/api/context-compaction/settings``.

Typed facade over the generic config DB (category ``context_compaction``)
so the dashboard gets defaults merged in and range validation on writes,
while audit history / export / import still come from config_service.

Lives on its own prefix (not ``/api/settings/context-compaction``) to
avoid colliding with the generic ``/api/settings/{category}`` route.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import verify_api_key
from services.context_compaction import (
    get_compaction_config,
    update_compaction_config,
)

logger = logging.getLogger("context_compaction_api")
router = APIRouter(prefix="/api/context-compaction", tags=["context-compaction"])


class CompactionSettingsPatch(BaseModel):
    """All fields optional — PATCH semantics. Range checks live in
    services.context_compaction.validate_config_updates (single source
    shared with any future non-HTTP caller)."""

    enabled: bool | None = None
    max_context_tokens: int | None = None
    compact_at_ratio: float | None = None
    keep_recent_messages: int | None = None
    max_tool_result_tokens_in_context: int | None = None
    min_savings_ratio: float | None = None
    snapshot_versions_visible: int | None = None
    emit_live_status: bool | None = None


@router.get("/settings", dependencies=[Depends(verify_api_key)])
async def get_settings() -> dict[str, Any]:
    return asdict(get_compaction_config())


@router.patch("/settings", dependencies=[Depends(verify_api_key)])
async def patch_settings(patch: CompactionSettingsPatch) -> dict[str, Any]:
    updates = {k: v for k, v in patch.model_dump().items() if v is not None}
    try:
        cfg = update_compaction_config(updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    logger.info("[COMPACT] Settings updated: %s", sorted(updates))
    return asdict(cfg)
