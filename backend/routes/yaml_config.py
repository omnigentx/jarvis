"""YAML config file API — ``/api/yaml/*``.

Allows the Settings UI to view and edit the two fast-agent YAML files that
live alongside the backend.  The route never touches arbitrary paths: only
the filenames listed in ``ALLOWED_FILES`` are acceptable, and the resolved
path must stay under the backend directory.  That keeps curl-style abuse
(``../../etc/passwd``) from ever reaching the filesystem.

Writes are validated with ``yaml.safe_load`` before hitting disk and the
previous content is kept as a ``.bak`` so a broken edit can be rolled back
manually if needed.
"""
from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import verify_api_key

logger = logging.getLogger("yaml_api")
router = APIRouter(prefix="/api/yaml", tags=["yaml"])

# Resolved once at import time; we freeze the base dir so even if someone
# flips cwd mid-request we never escape.
_BASE_DIR = Path(__file__).resolve().parent.parent

ALLOWED_FILES: dict[str, dict] = {
    "config": {
        "filename": "fastagent.config.yaml",
        "label": "fastagent.config.yaml",
        "description": "Main fast-agent configuration (models, MCP servers, logger).",
        "is_secret_file": False,
    },
    "secrets": {
        "filename": "fastagent.secrets.yaml",
        "label": "fastagent.secrets.yaml",
        "description": "Secrets and host-specific overrides (ignored by git).",
        "is_secret_file": True,
    },
}

# Caps to protect memory and dumb fat-finger edits; the UI should never send
# anything near these in normal use.
_MAX_BYTES = 256 * 1024


class YamlPutBody(BaseModel):
    content: str = Field(..., max_length=_MAX_BYTES)


def _resolve(name: str) -> Path:
    meta = ALLOWED_FILES.get(name)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Unknown YAML file: {name!r}")
    candidate = (_BASE_DIR / meta["filename"]).resolve()
    # Defence in depth: refuse anything that resolves outside the backend dir.
    if _BASE_DIR not in candidate.parents and candidate != _BASE_DIR:
        raise HTTPException(status_code=400, detail="Path traversal detected")
    return candidate


@router.get("/files", dependencies=[Depends(verify_api_key)])
async def list_files():
    return {
        "files": [
            {
                "name": name,
                "filename": meta["filename"],
                "label": meta["label"],
                "description": meta["description"],
                "exists": _resolve(name).exists(),
                "is_secret_file": meta["is_secret_file"],
            }
            for name, meta in ALLOWED_FILES.items()
        ]
    }


@router.get("/{name}", dependencies=[Depends(verify_api_key)])
async def read_file(name: str):
    path = _resolve(name)
    if not path.exists():
        # Allow the client to render an empty editor for files that haven't
        # been created yet (e.g. a fresh checkout without secrets.yaml).
        return {
            "name": name,
            "filename": ALLOWED_FILES[name]["filename"],
            "content": "",
            "exists": False,
            "size": 0,
        }
    try:
        data = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Read failed: {exc}") from exc
    return {
        "name": name,
        "filename": ALLOWED_FILES[name]["filename"],
        "content": data,
        "exists": True,
        "size": len(data.encode("utf-8")),
    }


@router.put("/{name}", dependencies=[Depends(verify_api_key)])
async def write_file(name: str, body: YamlPutBody):
    path = _resolve(name)

    # Parse once so we return a human-readable error instead of writing broken
    # YAML that breaks the backend on next restart.  Empty file is allowed —
    # `safe_load("")` returns None.
    try:
        yaml.safe_load(body.content)
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Invalid YAML — file not saved.",
                "error": str(exc),
            },
        )

    # Rotate previous content into a .bak sibling (atomic swap).
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            backup.write_bytes(path.read_bytes())
        except OSError as exc:
            logger.warning("[YAML] Could not write backup for %s: %s", path, exc)

    # Write atomically via tmp → rename so a crash mid-write can't leave a
    # half-written file that would tank the next startup.
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(body.content, encoding="utf-8")
        try:
            os.replace(tmp, path)
        except OSError as exc:
            # Docker bind-mounts a single file by inode — rename-over-mount
            # fails with EBUSY on Linux. Fall back to truncate + in-place
            # write; we keep the .bak from above as the rollback option.
            if exc.errno != errno.EBUSY:
                raise
            path.write_text(body.content, encoding="utf-8")
            try: tmp.unlink()
            except OSError: pass
    except OSError as exc:
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}") from exc

    logger.info("[YAML] %s saved (%d bytes)", path.name, len(body.content))
    return {
        "name": name,
        "filename": ALLOWED_FILES[name]["filename"],
        "size": len(body.content.encode("utf-8")),
        "saved": True,
    }
