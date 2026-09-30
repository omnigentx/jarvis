"""Authenticated runtime-plugin inventory and human-reviewed installation."""
from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import httpx
from core.auth import verify_api_key
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from services.plugins.activation import apply_skills
from services.plugins.installation import (
    PluginInstaller,
    approve_content,
    approve_source,
)
from services.plugins.lifecycle import PluginLifecycle, PluginStateError
from services.plugins.marketplace import discover
from services.plugins.package import PackageError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/plugins", tags=["plugins"], dependencies=[Depends(verify_api_key)])


class CatalogBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=160)


class InstallBody(CatalogBody):
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    subdirectory: str = Field(default="", max_length=512)


class ActivationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str = Field(min_length=1, max_length=128)


@lru_cache(maxsize=1)
def get_installer() -> PluginInstaller:
    from core.database import engine

    path = Path(engine.url.database).parent / "plugins"
    return PluginInstaller(PluginLifecycle(engine, path))


Installer = Annotated[PluginInstaller, Depends(get_installer)]


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    """Expose inventory, never MCP headers/environment/credentials."""
    result = {key: value for key, value in record.items() if key != "servers"}
    result["server_names"] = list(record.get("servers", {}))
    if record.get("bindings"):
        from services import shared_state

        bindings = []
        for binding in record["bindings"]:
            confirmed = binding["status"] != "ready"
            if not confirmed and shared_state.agent_app is not None:
                try:
                    agent = shared_state.agent_app.get_agent(binding["agent"])
                    expected = {record["name"] + ":" + skill["name"] for skill in record.get("skills", [])}
                    actual = {manifest.name for manifest in agent.skill_manifests
                              if record["id"] in manifest.path.parts and manifest.path.is_file()}
                    confirmed = bool(expected) and expected <= actual
                except (KeyError, ValueError, AttributeError, OSError):
                    confirmed = False
            bindings.append({**binding, "status": binding["status"] if confirmed else "needs_reactivation"})
        result["bindings"] = bindings
        if result["status"] == "ready" and any(binding["status"] != "ready" for binding in bindings):
            result["status"] = "needs_reactivation"
    return result


def api_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (PackageError, PluginStateError)):
        return HTTPException(409 if isinstance(exc, PluginStateError) else 422, str(exc))
    if isinstance(exc, httpx.HTTPError):
        logger.warning("[plugins] Source request failed type=%s", type(exc).__name__)
        return HTTPException(502, "Plugin source could not be retrieved")
    logger.warning("[plugins] Operation failed type=%s", type(exc).__name__)
    return HTTPException(500, "Plugin operation failed")


@router.get("")
async def list_plugins(installer: Installer):
    records = await asyncio.to_thread(installer.lifecycle.list)
    return {"plugins": [public_record(record) for record in records]}


@router.post("/catalog")
async def plugin_catalog(body: CatalogBody):
    try:
        return await discover(body.repo)
    except Exception as exc:
        raise api_error(exc) from exc


@router.post("/install")
async def install_plugin(body: InstallBody, installer: Installer):
    try:
        result = await installer.install(body.repo, body.commit, body.subdirectory, approve=approve_source)
        return public_record(result)
    except Exception as exc:
        raise api_error(exc) from exc


@router.post("/{identity}/activate")
async def activate_plugin(identity: str, body: ActivationBody, installer: Installer):
    async def approval(record):
        return await approve_content({**record, "target_agent": body.agent})
    try:
        result = await installer.lifecycle.activate(identity, body.agent, approve=approval, apply=apply_skills)
        return public_record(result)
    except Exception as exc:
        raise api_error(exc) from exc


@router.get("/{identity}/skills/{name}/content")
async def review_skill(identity: str, name: str, installer: Installer):
    """Explicit review of bounded skill content, without interpreting it."""
    try:
        record = installer.lifecycle.get(identity)
        skill = next((skill for skill in record["skills"] if skill["name"] == name), None)
        if skill is None:
            raise PluginStateError("Skill not found")
        from services.plugins.package import inspect_package

        root = installer.lifecycle.package_path(identity)
        package = await asyncio.to_thread(inspect_package, root)
        if package.digest != record["digest"]:
            raise PluginStateError("Package changed after inspection")
        content = await asyncio.to_thread((root / skill["path"]).read_text, encoding="utf-8")
        return {"name": name, "digest": record["digest"], "content": content}
    except Exception as exc:
        raise api_error(exc) from exc
