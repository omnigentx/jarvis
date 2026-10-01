"""Administrative plugin policy and explicit runtime target selection."""

from __future__ import annotations

import asyncio
import fcntl
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from core.auth import verify_api_key
from routes.plugins import Installer, api_error
from services.plugins.lifecycle import PluginStateError
from services.plugins.package import inspect_package
from services.plugins.policy import PluginPolicyStore, validate_policy

router = APIRouter(
    prefix="/api/plugins", tags=["plugins"], dependencies=[Depends(verify_api_key)]
)


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: str = Field(max_length=512)
    credentials: dict[str, Annotated[str, Field(max_length=8192)]] = Field(
        default_factory=dict, max_length=64
    )


@router.get("/runtime-profile")
async def plugin_runtime_profile():
    from services.plugins.runtime_profile import runtime_profile

    return await runtime_profile()


@router.get("/targets")
async def plugin_targets():
    from services import shared_state
    from services.plugins.targets import runtime_candidate

    targets = []
    app = shared_state.agent_app
    if app:
        for name in app._agents:
            targets.append({"agent": name, "run_id": None, "label": name})
    records = shared_state.registry_db.get_all() if shared_state.registry_db else {}
    for run_id, record in records.items():
        if runtime_candidate(record):
            name = record.get("agent_name")
            if name and record.get("session_id"):
                targets.append(
                    {
                        "agent": name,
                        "run_id": run_id,
                        "label": f"{name} · {record['session_id']}",
                    }
                )
    return {"targets": targets}


@router.get("/{identity}/policy")
async def plugin_policy(identity: str, installer: Installer):
    try:
        installer.lifecycle.get(identity)
        value = await asyncio.to_thread(
            PluginPolicyStore(installer.lifecycle.engine).get, identity
        )
        return {
            "image": value["image"] if value else None,
            "credential_slots": sorted(value["credentials"]) if value else [],
        }
    except Exception as exc:
        raise api_error(exc) from exc


@router.put("/{identity}/policy")
async def set_plugin_policy(identity: str, body: PolicyBody, installer: Installer):
    try:
        lifecycle = installer.lifecycle
        lifecycle.get(identity)
        root = lifecycle.package_path(identity)
        with lifecycle.operation_lock(identity).open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PluginStateError("Plugin operation already in progress") from exc
            record = lifecycle.get(identity)
            if record.get("global_enabled"):
                raise PluginStateError(
                    "Stop global sharing before changing execution policy"
                )
            if any(
                binding["status"]
                in {
                    "ready",
                    "activating",
                    "deactivating",
                    "activation_interrupted",
                    "detach_failed",
                }
                for binding in record["bindings"]
            ):
                raise PluginStateError(
                    "Disable and confirm removal before changing execution policy"
                )
            package = await asyncio.to_thread(inspect_package, root)
            if package.digest != record["digest"]:
                raise PluginStateError("Package changed after inspection")
            value = {"image": body.image, "credentials": body.credentials}
            validate_policy(root, package, value)
            await asyncio.to_thread(
                PluginPolicyStore(lifecycle.engine).put,
                identity,
                body.image,
                body.credentials,
            )
            return {
                "configured": True,
                "image": body.image,
                "credential_slots": sorted(body.credentials),
            }
    except Exception as exc:
        raise api_error(exc) from exc


@router.delete("/{identity}")
async def uninstall_plugin(identity: str, installer: Installer):
    try:
        result = await asyncio.to_thread(installer.lifecycle.uninstall, identity)
        from services.activity_stream import activity_stream_manager

        activity_stream_manager.broadcast(
            {
                "event_type": "plugin_status",
                "agent_name": "",
                "data": {"id": identity, "status": "expired"},
            }
        )
        return {"id": result["id"], "status": result["status"]}
    except Exception as exc:
        raise api_error(exc) from exc


@router.post("/{identity}/promote")
async def promote_plugin(identity: str, installer: Installer):
    try:
        from routes.plugins import verified_record
        from services import shared_state
        from services.plugins.dispatch import dispatch
        from services.plugins.installation import approve_content
        from services.plugins.sharing import promote
        from services.plugins.targets import runtime_candidate, team_binding

        installer.lifecycle.get(identity)
        targets = list(shared_state.agent_app._agents) if shared_state.agent_app else []
        records = shared_state.registry_db.get_all() if shared_state.registry_db else {}
        targets += [
            team_binding(record)
            for record in records.values()
            if record.get("session_id")
            and record.get("agent_name")
            and runtime_candidate(record)
        ]
        if len(set(targets)) > 32:
            raise PluginStateError(
                "Global sharing supports at most 32 current targets per operation"
            )

        async def apply(root, package, binding):
            return await dispatch(
                root, package, binding, engine=installer.lifecycle.engine
            )

        result = await promote(
            installer.lifecycle, identity, targets, approve=approve_content, apply=apply
        )
        return await verified_record(result, installer)
    except Exception as exc:
        raise api_error(exc) from exc


@router.post("/{identity}/stop-sharing")
async def stop_plugin_sharing(identity: str, installer: Installer):
    try:
        from routes.plugins import verified_record
        from services.plugins.sharing import stop_sharing

        stop_sharing(installer.lifecycle, identity)
        return await verified_record(installer.lifecycle.get(identity), installer)
    except Exception as exc:
        raise api_error(exc) from exc


@router.get("/{identity}/files")
async def review_plugin_files(
    identity: str, installer: Installer, path: str | None = None
):
    try:
        from services.plugins.review import review_package

        if path is not None and len(path) > 4096:
            raise PluginStateError("Invalid package review path")
        return await asyncio.to_thread(
            review_package, installer.lifecycle, identity, path
        )
    except Exception as exc:
        raise api_error(exc) from exc
