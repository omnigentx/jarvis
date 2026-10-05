"""Human-only remote-account consent, distinct from agent activation review."""

from __future__ import annotations

import asyncio
import fcntl
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from core.auth import verify_api_key
from routes.plugins import Installer, api_error
from services.plugins.lifecycle import PluginStateError
from services.plugins.package import inspect_package
from services.plugins.policy import PluginPolicyStore
from services.plugins.remote_auth import (
    REMOTE_POLICY,
    RemoteTokenStore,
    approved_endpoint,
)
from services.plugins.remote_connection import remote_connections

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


class ConnectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server: str = Field(max_length=128)
    redirect_uri: str = Field(max_length=512)


def configuration_allowed(record: dict):
    if record.get("global_enabled") or any(
        b["status"]
        in {
            "ready",
            "activating",
            "deactivating",
            "activation_interrupted",
            "detach_failed",
        }
        for b in record.get("bindings", [])
    ):
        raise PluginStateError(
            "Disable current bindings and global sharing before reconnecting"
        )
    if record["status"] == "expired":
        raise PluginStateError("Plugin package has expired")


@router.post("/{identity}/remote/connect", dependencies=[Depends(verify_api_key)])
async def connect_remote(identity: str, body: ConnectBody, installer: Installer):
    try:
        lifecycle = installer.lifecycle
        record = lifecycle.get(identity)
        configuration_allowed(record)
        root = lifecycle.package_path(identity)
        package = await asyncio.to_thread(inspect_package, root)
        if package.digest != record["digest"]:
            raise PluginStateError("Package changed after inspection")
        if set(package.blockers) - {"mcp_requires_policy_review"}:
            raise PluginStateError("This package needs an unsupported adapter")
        endpoint = approved_endpoint(package.servers[body.server])
        store = RemoteTokenStore(lifecycle.engine, identity, body.server, endpoint)

        async def connected():
            with lifecycle.operation_lock(identity).open("a") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise PluginStateError(
                        "Plugin operation already in progress"
                    ) from exc
                current = lifecycle.get(identity)
                configuration_allowed(current)
                if (
                    not root.is_dir()
                    or inspect_package(root).digest != record["digest"]
                ):
                    raise PluginStateError(
                        "Package changed during account authorization"
                    )
                stores = [
                    RemoteTokenStore(
                        lifecycle.engine, identity, name, approved_endpoint(config)
                    )
                    for name, config in package.servers.items()
                ]
                if all(
                    s.read().get("tokens") and s.read().get("client") for s in stores
                ):
                    PluginPolicyStore(lifecycle.engine).put(
                        identity,
                        REMOTE_POLICY,
                        {
                            "_database": str(lifecycle.engine.url.database),
                            "_runtime_root": str(lifecycle.root),
                        },
                    )

        return await remote_connections.start(
            store, body.redirect_uri, connected=connected
        )
    except Exception as exc:
        raise api_error(exc) from exc


@router.delete(
    "/{identity}/remote/connect/{flow_id}", dependencies=[Depends(verify_api_key)]
)
async def cancel_remote(identity: str, flow_id: str, installer: Installer):
    installer.lifecycle.get(identity)
    flow = remote_connections.flows.get(flow_id)
    if flow and flow.store.candidate != identity:
        raise api_error(PluginStateError("Flow belongs to another package"))
    await remote_connections.cancel(flow_id)
    return {"cancelled": True}


@router.get("/{identity}/remote", dependencies=[Depends(verify_api_key)])
async def remote_status(identity: str, installer: Installer):
    try:
        record = installer.lifecycle.get(identity)
        servers = []
        for name, config in record["servers"].items():
            endpoint = approved_endpoint(config)
            if record["status"] == "expired":
                servers.append(
                    {
                        "name": name,
                        "url": endpoint,
                        "status": "disconnected",
                        "flow_id": None,
                    }
                )
                continue
            store = RemoteTokenStore(
                installer.lifecycle.engine, identity, name, endpoint
            )
            flow = next(
                (
                    f
                    for f in remote_connections.flows.values()
                    if f.store.candidate == identity and f.store.server == name
                ),
                None,
            )
            servers.append(
                {
                    "name": name,
                    "url": endpoint,
                    "status": "connecting"
                    if flow
                    else "connected"
                    if store.read().get("tokens")
                    else "disconnected",
                    "flow_id": flow.id if flow else None,
                }
            )
        return {"servers": servers}
    except Exception as exc:
        raise api_error(exc) from exc


@router.delete("/{identity}/remote", dependencies=[Depends(verify_api_key)])
async def disconnect_remote(identity: str, installer: Installer):
    """Remove local account credentials only after all bindings are disabled."""
    try:
        lifecycle = installer.lifecycle
        with lifecycle.operation_lock(identity).open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PluginStateError("Plugin operation already in progress") from exc
            configuration_allowed(lifecycle.get(identity))
            for flow in list(remote_connections.flows.values()):
                if flow.store.candidate == identity:
                    await remote_connections.cancel(flow.id)
            PluginPolicyStore(lifecycle.engine).delete(identity)
            from services.activity_stream import activity_stream_manager

            for server in lifecycle.get(identity)["servers"]:
                activity_stream_manager.broadcast(
                    {
                        "event_type": "plugin_status",
                        "agent_name": "",
                        "data": {
                            "id": identity,
                            "remote_server": server,
                            "remote_status": "disconnected",
                            "policy_configured": False,
                        },
                    }
                )
        return {"disconnected": True}
    except Exception as exc:
        raise api_error(exc) from exc


@router.get("/oauth/callback")
async def remote_callback(
    state: str = "", code: str | None = None, error: str | None = None
):
    # State/PKCE bind this public redirect to an authenticated start. Neither
    # authorization code nor remote error text is reflected into HTML or logs.
    try:
        if len(state) > 512 or (code and len(code) > 4096):
            raise PluginStateError("Invalid authorization callback")
        await remote_connections.complete(state, code, error)
        return HTMLResponse(
            '<!doctype html><title>Jarvis</title><p>Authorization received. Return to Jarvis to see the connection result.</p><script>history.replaceState(null,"",location.pathname);window.close()</script>',
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    except Exception:
        return HTMLResponse(
            "<!doctype html><title>Jarvis</title><p>Authorization expired or denied. Reconnect from Settings.</p>",
            status_code=400,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
