"""Settings REST API — ``/api/settings/*``.

A thin HTTP facade over :mod:`services.config_service`.  All routes are
bearer-token protected; secrets are always masked in read responses.

Route-order note
----------------
FastAPI matches routes in registration order, so the fixed paths
(``/history``, ``/bulk``) **must** be declared before the dynamic
``/{category}`` / ``/{category}/{key}`` paths — otherwise the dynamic path
swallows them.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core import auth as core_auth
from core.auth import verify_api_key
from services.config_service import ConfigEntry, config_service
from services.runtime_config import apply_master_key

logger = logging.getLogger("settings_api")
router = APIRouter(prefix="/api/settings", tags=["settings"])


# ---- Schemas -----------------------------------------------------------------


class SetValue(BaseModel):
    value: Optional[str] = None
    is_secret: bool = False


class BulkItem(BaseModel):
    category: str = Field(min_length=1, max_length=100)
    key: str = Field(min_length=1, max_length=100)
    value: Optional[str] = None
    is_secret: bool = False


class BulkUpdate(BaseModel):
    items: list[BulkItem] = Field(min_length=1, max_length=200)


class ImportBody(BaseModel):
    """Payload accepted by the import endpoint.

    ``version`` is a plain integer today but exists so future schema
    changes can be detected and refused rather than silently misapplied.
    ``items`` uses the same tuple-shape the bulk endpoint already accepts.
    """

    version: int = Field(ge=1, le=99)
    items: list[BulkItem] = Field(min_length=1, max_length=500)
    # If true, any *category* present in the file replaces the category
    # on disk (deletes extras).  Default is merge — safer default, less
    # likely to nuke a user's carefully-set keys.
    replace: bool = False


_EXPORT_VERSION = 1
# Don't dump sensitive data even in plaintext form when exporting; a user
# copying an export file into a ticket shouldn't leak their API keys.
_SECRET_EXPORT_PLACEHOLDER = "__SECRET__"


def _entry_to_dict(entry: ConfigEntry) -> dict:
    return asdict(entry)


def _maybe_apply_master_key(category: str, key: str, new_value: Optional[str]) -> None:
    """If ``auth.JARVIS_API_KEY`` just changed, propagate to the running process."""
    if category == "auth" and key == "JARVIS_API_KEY" and new_value:
        apply_master_key(new_value)


# ---- Fixed paths (registered first) -----------------------------------------


@router.get("/history", dependencies=[Depends(verify_api_key)])
async def list_history(
    category: Optional[str] = None,
    key: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
):
    rows = config_service.get_history(category=category, key=key, limit=limit)
    return {
        "items": [
            {
                "id": r.id,
                "category": r.category,
                "key": r.key,
                "old_value": r.old_value,
                "new_value": r.new_value,
                "is_secret": r.is_secret,
                "action": r.action,
                "changed_at": r.changed_at,
                "changed_by": r.changed_by,
            }
            for r in rows
        ]
    }


@router.get("/export", dependencies=[Depends(verify_api_key)])
async def export_settings(include_secrets: bool = False):
    """Dump every stored config entry in a portable JSON envelope.

    Secrets are replaced with ``__SECRET__`` by default so an exported
    file is safe to share.  Setting ``include_secrets=true`` opts into
    plaintext export — useful for backups, never for sharing.
    """
    grouped = config_service.list_all()
    items: list[dict] = []
    for category, entries in grouped.items():
        for entry in entries:
            if entry.is_secret and not include_secrets:
                value: Optional[str] = _SECRET_EXPORT_PLACEHOLDER if entry.has_value else None
            elif entry.is_secret and include_secrets:
                plain = config_service.get(category, entry.key)
                value = plain
            else:
                value = entry.value
            items.append(
                {
                    "category": category,
                    "key": entry.key,
                    "value": value,
                    "is_secret": entry.is_secret,
                }
            )
    return {"version": _EXPORT_VERSION, "items": items, "includes_secrets": include_secrets}


@router.post("/import", dependencies=[Depends(verify_api_key)])
async def import_settings(payload: ImportBody):
    """Apply an exported file to the running config.

    Rules:
    * Entries whose value is the ``__SECRET__`` placeholder are
      **skipped** — we never want to overwrite a real secret with a
      sentinel.
    * When ``replace=True``, any existing key in a category that's
      present in the payload but *missing* from the payload gets deleted
      so the post-state matches the file exactly.
    """
    _preserved = _SECRET_EXPORT_PLACEHOLDER
    apply_items: list[tuple[str, str, Optional[str], bool]] = []
    skipped: list[str] = []
    seen_by_category: dict[str, set[str]] = {}

    for it in payload.items:
        seen_by_category.setdefault(it.category, set()).add(it.key)
        if it.value == _preserved:
            skipped.append(f"{it.category}/{it.key}")
            continue
        apply_items.append((it.category, it.key, it.value, it.is_secret))

    deletions: list[tuple[str, str, Optional[str], bool]] = []
    if payload.replace:
        existing = config_service.list_all()
        for category, present_keys in seen_by_category.items():
            for entry in existing.get(category, []):
                if entry.key not in present_keys:
                    deletions.append((category, entry.key, None, entry.is_secret))

    try:
        events = config_service.set_many(apply_items + deletions, source="import")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    for ev in events:
        _maybe_apply_master_key(ev.category, ev.key, ev.new_value)

    return {
        "applied": len(apply_items),
        "deleted": len(deletions),
        "skipped_secrets": skipped,
    }


@router.post("/bulk", dependencies=[Depends(verify_api_key)])
async def bulk_update(payload: BulkUpdate):
    try:
        events = config_service.set_many(
            [(i.category, i.key, i.value, i.is_secret) for i in payload.items],
            source="user",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    for ev in events:
        _maybe_apply_master_key(ev.category, ev.key, ev.new_value)

    return {
        "events": [
            {
                "category": e.category,
                "key": e.key,
                "action": e.action,
                "is_secret": e.is_secret,
            }
            for e in events
        ]
    }


@router.get("", dependencies=[Depends(verify_api_key)])
async def list_all():
    grouped = config_service.list_all()
    return {
        "categories": {
            cat: [_entry_to_dict(e) for e in entries]
            for cat, entries in grouped.items()
        }
    }


# ---- Dynamic paths (registered last) ----------------------------------------


@router.get("/{category}", dependencies=[Depends(verify_api_key)])
async def list_category(category: str):
    entries = config_service.list_category(category)
    return {"category": category, "items": [_entry_to_dict(e) for e in entries]}


@router.get("/{category}/{key}", dependencies=[Depends(verify_api_key)])
async def get_entry(category: str, key: str):
    entry = config_service.get_entry(category, key)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _entry_to_dict(entry)


@router.put("/{category}/{key}", dependencies=[Depends(verify_api_key)])
async def put_entry(category: str, key: str, payload: SetValue):
    try:
        event = config_service.set(
            category, key, payload.value, is_secret=payload.is_secret
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    _maybe_apply_master_key(event.category, event.key, event.new_value)

    return {
        "category": event.category,
        "key": event.key,
        "action": event.action,
        "is_secret": event.is_secret,
    }


@router.delete("/{category}/{key}", dependencies=[Depends(verify_api_key)])
async def delete_entry(category: str, key: str):
    # Prevent the user from accidentally locking themselves out via a routine
    # delete on the master key.  Clearing it requires the bulk endpoint (which
    # the Settings UI never exposes that combination).
    if category == "auth" and key == "JARVIS_API_KEY":
        raise HTTPException(
            status_code=400,
            detail="Refusing to delete the master key via DELETE; rotate via PUT instead.",
        )
    hit = config_service.delete(category, key)
    if not hit:
        raise HTTPException(status_code=404, detail="Not found")
    return {"deleted": True, "category": category, "key": key}
