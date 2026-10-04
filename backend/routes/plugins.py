"""Authenticated runtime-plugin inventory and human-reviewed installation."""

from __future__ import annotations

import asyncio
import logging
import re
from functools import lru_cache
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from core.auth import verify_api_key
from services.plugins.dispatch import dispatch
from services.plugins.installation import (
    PluginInstaller,
    approve_source,
)
from services.plugins.lifecycle import PluginStateError
from services.plugins.manual_review import issue_review
from services.plugins.marketplace import discover
from services.plugins.package import PackageError
from services.plugins.policy import PluginPolicyStore
from services.plugins.targets import resolve_target

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/plugins", tags=["plugins"], dependencies=[Depends(verify_api_key)]
)


class CatalogBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=160)


class InstallBody(CatalogBody):
    source_confirmed: bool = False
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    subdirectory: str = Field(default="", max_length=512)


class ActivationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str = Field(min_length=1, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)


@lru_cache(maxsize=1)
def get_installer() -> PluginInstaller:
    from services.plugins.operations import runtime_installer

    return runtime_installer()


Installer = Annotated[PluginInstaller, Depends(get_installer)]


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    """Expose inventory, never MCP headers/environment/credentials."""
    result = {key: value for key, value in record.items() if key != "servers"}
    from services.plugins.provenance import source_origin

    result["source_origin"] = source_origin(record.get("repo", ""))
    result["server_names"] = list(record.get("servers", {}))
    result["remote_servers"] = [
        {"name": name, "url": server.get("url"), "transport": server.get("transport")}
        for name, server in record.get("servers", {}).items()
        if server.get("transport") == "http"
    ]
    result["credential_slots"] = sorted(
        {
            value[2:-1]
            for server in record.get("servers", {}).values()
            for value in server.get("env", {}).values()
            if isinstance(value, str)
            and re.fullmatch(r"\$\{[A-Z_][A-Z0-9_]{0,127}\}", value)
        }
    )
    if record.get("bindings"):
        from services import shared_state

        bindings = []
        for binding in record["bindings"]:
            confirmed = binding["status"] != "ready"
            if not confirmed and shared_state.agent_app is not None:
                try:
                    agent = shared_state.agent_app.get_agent(binding["agent"])
                    expected = {
                        record["name"] + ":" + skill["name"]
                        for skill in record.get("skills", [])
                    }
                    actual = {
                        manifest.name
                        for manifest in agent.skill_manifests
                        if record["id"] in manifest.path.parts
                        and manifest.path.is_file()
                    }
                    from services.plugins.mcp_runtime import server_names_for

                    expected_servers = set(
                        server_names_for(record["digest"], record.get("servers", {}))
                    )
                    confirmed = (
                        bool(expected or expected_servers) and expected <= actual
                    )
                    if expected_servers:
                        confirmed = confirmed and expected_servers <= set(
                            agent.list_attached_mcp_servers()
                        )
                except (KeyError, ValueError, AttributeError, OSError):
                    confirmed = False
            bindings.append(
                {
                    **binding,
                    "status": binding["status"] if confirmed else "needs_reactivation",
                }
            )
        result["bindings"] = bindings
        if result["status"] == "ready" and any(
            binding["status"] == "needs_reactivation" for binding in bindings
        ):
            result["status"] = "needs_reactivation"
    return result


def api_error(exc: Exception) -> HTTPException:
    if isinstance(exc, TimeoutError):
        return HTTPException(
            504, "Plugin operation timed out; refresh inventory before retrying"
        )
    if isinstance(exc, (PackageError, PluginStateError)):
        return HTTPException(
            409 if isinstance(exc, PluginStateError) else 422, str(exc)
        )
    if isinstance(exc, httpx.HTTPError):
        logger.warning("[plugins] Source request failed type=%s", type(exc).__name__)
        return HTTPException(502, "Plugin source could not be retrieved")
    logger.warning("[plugins] Operation failed type=%s", type(exc).__name__)
    return HTTPException(500, "Plugin operation failed")


async def verified_record(
    record: dict[str, Any], installer: PluginInstaller
) -> dict[str, Any]:
    result = public_record(record)
    # Reinspect semantic capabilities after adapter upgrades; package bytes
    # and the reviewed digest remain immutable.
    from services.plugins.package import inspect_package

    if record.get("digest"):
        try:
            package = await asyncio.to_thread(
                inspect_package, installer.lifecycle.package_path(record["id"])
            )
            if package.digest == record["digest"]:
                result["blockers"] = list(package.blockers)
            else:
                result["status"] = "integrity_failed"
        except (PackageError, OSError):
            result["status"] = "integrity_failed"
    if record.get("servers"):
        try:
            configured = PluginPolicyStore(installer.lifecycle.engine).get(record["id"])
            result["policy_configured"] = configured is not None
        except Exception:  # noqa: BLE001 — runtime boundary; do not expose credential-bearing errors
            result["policy_configured"] = False

    async def verify(binding):
        if not binding["agent"].startswith("team:"):
            return None
        from services.plugins.targets import matching_team_records

        records = matching_team_records(binding["agent"])
        selected = (
            max(records, key=lambda item: item.get("started_at") or 0)
            if records
            else {}
        )
        if binding["status"] != "ready":
            return {
                **binding,
                "run_id": selected.get("run_id"),
                "agent_name": selected.get("agent_name"),
            }
        try:
            from services.plugins.ipc import request_update, socket_path
            from services.plugins.targets import live_team_run

            target = live_team_run(binding["agent"])
            database = str(installer.lifecycle.engine.url.database)
            ack = await request_update(
                socket_path(database, target["run_id"]),
                run_id=target["run_id"],
                candidate=record["id"],
                digest=record["digest"],
                operation="status",
                timeout=2,
            )
            return {
                **binding,
                "status": "ready" if ack else "needs_reactivation",
                "run_id": target["run_id"],
                "agent_name": target["agent_name"],
            }
        except Exception:  # noqa: BLE001 — runtime boundary; do not expose credential-bearing errors
            from services.plugins.targets import matching_team_records

            records = matching_team_records(binding["agent"])
            selected = (
                max(records, key=lambda item: item.get("started_at") or 0)
                if records
                else {}
            )
            return {
                **binding,
                "status": "needs_reactivation",
                "run_id": selected.get("run_id"),
                "agent_name": selected.get("agent_name"),
            }

    checked = await asyncio.gather(
        *(verify(binding) for binding in record.get("bindings", []))
    )
    for index, binding in enumerate(checked):
        if binding is not None:
            result["bindings"][index] = binding
    if record["status"] == "ready" and result["status"] != "integrity_failed":
        result["status"] = (
            "ready"
            if not any(
                binding["status"] == "needs_reactivation"
                for binding in result.get("bindings", [])
            )
            else "needs_reactivation"
        )
    return result


@router.get("")
async def list_plugins(installer: Installer):
    records = await asyncio.to_thread(installer.lifecycle.list)
    return {
        "plugins": await asyncio.gather(
            *(verified_record(record, installer) for record in records)
        )
    }


@router.post("/catalog")
async def plugin_catalog(body: CatalogBody):
    try:
        return await discover(body.repo)
    except Exception as exc:
        raise api_error(exc) from exc


@router.post("/install")
async def install_plugin(body: InstallBody, installer: Installer):
    try:
        result = await installer.install(
            body.repo,
            body.commit,
            body.subdirectory,
            approve=confirmed_source if body.source_confirmed else approve_source,
        )
        return public_record(result)
    except Exception as exc:
        raise api_error(exc) from exc


@router.post("/{identity}/activate")
async def activate_plugin(identity: str, body: ActivationBody, installer: Installer):
    try:
        binding = resolve_target(body.agent, body.run_id)
        from services.plugins.operations import activate_binding

        result = await activate_binding(identity, binding, installer)
        return await verified_record(result, installer)
    except Exception as exc:
        raise api_error(exc) from exc


@router.post("/{identity}/deactivate")
async def deactivate_plugin(identity: str, body: ActivationBody, installer: Installer):
    try:
        binding = resolve_target(body.agent, body.run_id, remove=True)

        async def remove(root, package, target):
            return await dispatch(
                root, package, target, engine=installer.lifecycle.engine, remove=True
            )

        return public_record(
            await installer.lifecycle.deactivate(identity, binding, remove=remove)
        )
    except Exception as exc:
        raise api_error(exc) from exc


@router.get("/{identity}/skills/{name}/content")
async def review_skill(identity: str, name: str, installer: Installer):
    """Explicit review of bounded skill content, without interpreting it."""
    try:
        record = installer.lifecycle.get(identity)
        skill = next(
            (skill for skill in record["skills"] if skill["name"] == name), None
        )
        if skill is None:
            raise PluginStateError("Skill not found")
        from services.plugins.package import inspect_package

        root = installer.lifecycle.package_path(identity)
        package = await asyncio.to_thread(inspect_package, root)
        if package.digest != record["digest"]:
            raise PluginStateError("Package changed after inspection")
        content = await asyncio.to_thread(
            (root / skill["path"]).read_text, encoding="utf-8"
        )
        return {"name": name, "digest": record["digest"], "content": content}
    except Exception as exc:
        raise api_error(exc) from exc


async def confirmed_source(record: dict[str, Any]) -> bool:
    """The authenticated user explicitly confirmed this exact install body."""
    return True


class ManualActivationBody(ActivationBody):
    review_token: str = Field(min_length=1, max_length=4096)


@router.post("/{identity}/manual-review")
async def review_activation(identity: str, body: ActivationBody, installer: Installer):
    try:
        binding = resolve_target(body.agent, body.run_id)
        record = installer.lifecycle.get(identity)
        policy = PluginPolicyStore(installer.lifecycle.engine).get(identity)
        from services.plugins.transport import policy_summary

        claims = {
            "id": identity,
            "digest": record["digest"],
            "target": binding,
            "policy_revision": policy["revision"] if policy else "",
        }
        return {
            "plugin": public_record(record),
            "target": body.agent,
            "run_id": body.run_id,
            "execution_policy": policy_summary(policy, record.get("servers", {})),
            "review_token": issue_review(claims),
        }
    except Exception as exc:
        raise api_error(exc) from exc


@router.post("/{identity}/manual-activate")
async def confirm_activation(
    identity: str, body: ManualActivationBody, installer: Installer
):
    try:
        from services.plugins.operations import activate_binding

        binding = resolve_target(body.agent, body.run_id)
        result = await activate_binding(
            identity,
            binding,
            installer,
            requested_by="User",
            target_label=body.agent,
            manual_review=body.review_token,
        )
        return await verified_record(result, installer)
    except Exception as exc:
        raise api_error(exc) from exc
