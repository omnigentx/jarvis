"""MCP server management API — ``/api/mcp/*``.

Thin shell over ``services.mcp_catalog``, ``services.mcp_attachments`` and the
``services.mcp_runtime.audit`` instrumentation. Auth via ``verify_api_key``
(same as the rest of the dashboard APIs).

Built-in servers (seeded from fastagent.config.yaml) are editable but cannot
be deleted — the catalog layer raises ``PermissionError`` which we map to 403.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from core.auth import verify_api_key
from core.database import McpEventLogModel, SessionLocal
from helpers.http_errors import safe_500
from services import mcp_attachments, mcp_catalog
from services.activity_stream import activity_stream_manager
from services.mcp_runtime import audit

logger = logging.getLogger("mcp_api")
router = APIRouter(prefix="/api/mcp", tags=["mcp"])


# ── Bodies ────────────────────────────────────────────────────────────


class ServerCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    transport: str = Field(..., pattern=r"^(stdio|http|sse)$")
    command: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    url: Optional[str] = None
    cwd: Optional[str] = None


class ServerUpdateBody(BaseModel):
    transport: Optional[str] = Field(None, pattern=r"^(stdio|http|sse)$")
    command: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    url: Optional[str] = None
    cwd: Optional[str] = None


class TestBody(BaseModel):
    transport: str = Field(..., pattern=r"^(stdio|http|sse)$")
    command: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    url: Optional[str] = None
    cwd: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────


def _enrich_with_status(server: dict[str, Any]) -> dict[str, Any]:
    """Add live status + attached_agents."""
    server["attached_agents"] = mcp_attachments.list_for_server(server["name"])
    server["status"] = _live_status(server["name"])
    return server


def _live_status(server_name: str) -> str:
    """Look up the server connection state across all running aggregators."""
    from services import shared_state

    agent_app = shared_state.agent_app
    if agent_app is None:
        return "unknown"
    seen_running = False
    for ag_name in getattr(agent_app, "agents", {}):
        try:
            ag = agent_app.get_agent(ag_name)
            agg = getattr(ag, "_aggregator", None)
            if not agg or not agg._persistent_connection_manager:
                continue
            conn = agg._persistent_connection_manager.running_servers.get(server_name)
            if conn:
                if conn.is_healthy():
                    seen_running = True
                else:
                    return "error"
        except Exception:
            continue
    return "running" if seen_running else "stopped"


# ── Catalog endpoints ─────────────────────────────────────────────────


@router.get("/servers", dependencies=[Depends(verify_api_key)])
async def list_servers() -> dict[str, Any]:
    items = [_enrich_with_status(s) for s in mcp_catalog.list_all(mask_secrets=True)]
    return {"servers": items}


@router.get("/servers/{name}", dependencies=[Depends(verify_api_key)])
async def get_server(name: str) -> dict[str, Any]:
    server = mcp_catalog.get(name, mask_secrets=True)
    if not server:
        raise HTTPException(status_code=404, detail={"message": f"server {name!r} not found"})
    server = _enrich_with_status(server)
    # Cached tools from mcp_server_tools (best-effort).
    # registry_db.get_server_tools takes a list[str] and returns
    # dict[server_name, list[{name, description}]].
    try:
        from services import shared_state
        if shared_state.registry_db:
            tools_by_server = shared_state.registry_db.get_server_tools([name])
            server["tools"] = tools_by_server.get(name, [])
        else:
            server["tools"] = []
    except Exception:
        server["tools"] = []
    return server


@router.get("/servers/{name}/secret/{env_key}", dependencies=[Depends(verify_api_key)])
async def reveal_secret(name: str, env_key: str) -> dict[str, Any]:
    """Reveal a single env value (for the UI eye icon).

    Logs an audit row per reveal — every other mutation/read on a server
    goes through ``audit()``; secret reveal was the one gap. Detail records
    the env key, never the value.
    """
    async with audit("reveal_secret", server=name, actor="user",
                     detail={"env_key": env_key}):
        val = mcp_catalog.get_secret_value(name, env_key)
    if val is None:
        raise HTTPException(status_code=404, detail={"message": "env key not found"})
    return {"name": name, "key": env_key, "value": val}


@router.post("/servers", status_code=201, dependencies=[Depends(verify_api_key)])
async def create_server(body: ServerCreateBody) -> dict[str, Any]:
    payload = body.model_dump(exclude_none=True)
    name = payload.pop("name")
    try:
        return _enrich_with_status(await mcp_catalog.create(name, payload))
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"message": str(e)})
    except RuntimeError as e:
        # smoke test failed
        raise HTTPException(status_code=400, detail={"message": str(e), "smoke_failed": True})


@router.put("/servers/{name}", dependencies=[Depends(verify_api_key)])
async def update_server(name: str, body: ServerUpdateBody) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True)
    try:
        result = await mcp_catalog.update(name, patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"message": str(e)})
    except LookupError as e:
        raise HTTPException(status_code=404, detail={"message": str(e)})
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail={"message": str(e), "smoke_failed": True})

    # Fan out reconnect. If any agent failed to reconnect, return 207 as the
    # actual HTTP status (not just a body field) so HTTP clients see the
    # partial-failure signal. partial_failure mirrors fanout.all_ok=False
    # for clients that don't read status_code.
    fanout = await mcp_attachments.reconnect_all_for_server(name)
    payload = _enrich_with_status(result)
    payload["fanout"] = fanout
    if not fanout["all_ok"]:
        payload["partial_failure"] = True
        return JSONResponse(status_code=207, content=payload)
    return payload


@router.delete("/servers/{name}", dependencies=[Depends(verify_api_key)])
async def delete_server(name: str) -> dict[str, Any]:
    # Live-detach from every agent FIRST so aggregators are clean before catalog row goes
    detach_results = await mcp_attachments.detach_from_all(name)
    try:
        result = await mcp_catalog.delete(name)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail={"message": str(e)})
    except LookupError as e:
        raise HTTPException(status_code=404, detail={"message": str(e)})
    result["detached_from"] = [r["agent"] for r in detach_results]
    return result


@router.post("/servers/{name}/test", dependencies=[Depends(verify_api_key)])
async def test_existing_server(name: str) -> dict[str, Any]:
    """Smoke-test a saved server. On success, refresh mcp_server_tools cache —
    serves as the user-facing "refresh tools" trigger for servers that no
    agent currently attaches (fast-agent only connects to attached servers
    at boot, so unattached servers' tool cache goes stale after edits)."""
    server = mcp_catalog.get(name, mask_secrets=False)
    if not server:
        raise HTTPException(status_code=404, detail={"message": f"server {name!r} not found"})
    result = await mcp_catalog.smoke_test(server, return_tool_details=True)
    if result.get("ok") and result.get("tool_details"):
        try:
            from services import shared_state
            if shared_state.registry_db:
                shared_state.registry_db.upsert_server_tools(name, result["tool_details"])
        except Exception:
            logger.exception("[mcp.test] failed to refresh tools cache for %s", name)
    return result


@router.post("/servers/test", dependencies=[Depends(verify_api_key)])
async def test_unsaved_server(body: TestBody) -> dict[str, Any]:
    """Smoke-test an unsaved config (used by the UI's 'Test' button before save)."""
    payload = body.model_dump(exclude_none=True)
    return await mcp_catalog.smoke_test(payload)


# ── Attachment endpoints ──────────────────────────────────────────────


@router.post("/servers/{name}/agents/{agent}", dependencies=[Depends(verify_api_key)])
async def attach_to_agent(name: str, agent: str) -> dict[str, Any]:
    try:
        return await mcp_attachments.attach(agent, name)
    except LookupError as e:
        raise HTTPException(status_code=404, detail={"message": str(e)})
    except Exception as e:
        raise safe_500(e, logger, "mcp_attach_failed") from e


@router.delete("/servers/{name}/agents/{agent}", dependencies=[Depends(verify_api_key)])
async def detach_from_agent(name: str, agent: str) -> dict[str, Any]:
    return await mcp_attachments.detach(agent, name)


@router.get("/attachments", dependencies=[Depends(verify_api_key)])
async def list_attachments() -> dict[str, Any]:
    return {"attachments": mcp_attachments.list_all()}


# ── Audit / events endpoints ──────────────────────────────────────────


@router.get("/events", dependencies=[Depends(verify_api_key)])
async def list_events(
    server: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    since: Optional[float] = Query(None, description="UNIX seconds"),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    """Query the audit log. Newest first."""
    with SessionLocal() as db:
        stmt = select(McpEventLogModel).order_by(desc(McpEventLogModel.timestamp))
        if server:
            stmt = stmt.where(McpEventLogModel.server_name == server)
        if agent:
            stmt = stmt.where(McpEventLogModel.agent_name == agent)
        if action:
            stmt = stmt.where(McpEventLogModel.action == action)
        if since:
            stmt = stmt.where(McpEventLogModel.timestamp >= since)
        rows = db.execute(stmt.limit(limit)).scalars().all()
        out = [
            {
                "id": r.id,
                "timestamp": r.timestamp,
                "action": r.action,
                "server": r.server_name,
                "agent": r.agent_name,
                "actor": r.actor,
                "outcome": r.outcome,
                "duration_ms": r.duration_ms,
                "detail": json.loads(r.detail_json) if r.detail_json else {},
            }
            for r in rows
        ]
    return {"events": out}


@router.get("/events/stream", dependencies=[Depends(verify_api_key)])
async def stream_events():
    """SSE stream of MCP events only (filtered from activity_stream_manager).

    Simpler than reusing /api/agents/activity-stream which carries all event types.
    """
    sub_id, queue = activity_stream_manager.subscribe(agent_filter=None)

    async def event_gen():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event.get("type") != "mcp":
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            activity_stream_manager.unsubscribe(sub_id)

    return StreamingResponse(event_gen(), media_type="text/event-stream")
