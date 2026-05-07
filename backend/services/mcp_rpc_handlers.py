"""RPC handlers for Jarvis-driven MCP catalog management.

These handlers run in the main backend process so they can mutate the live
catalog (``mcp_catalog``), the per-agent allowlist (``mcp_attachments``),
the build pipeline state (``mcp_admin_service``), and broadcast audit events
to the dashboard activity stream — all without round-tripping through HTTP.

The companion ``tools/mcp_admin_server.py`` MCP subprocess wraps each method
as an LLM-facing tool.
"""
from __future__ import annotations

import logging
from typing import Any

from services import mcp_admin_service as admin
from services import mcp_attachments, mcp_catalog
from services.runtime_rpc import RuntimeRpcServer

logger = logging.getLogger("mcp_rpc")

# Names Jarvis is NOT allowed to mutate via its own tools — would lock
# itself out of the catalog admin loop. Dashboard (HTTP route) is unaffected.
_SELF_LOCKED = {"mcp_admin", "skill_server"}


def _self_lockout(name: str, op: str) -> dict | None:
    if name in _SELF_LOCKED:
        return {
            "error": (
                f"{op} {name!r} is blocked via Jarvis tools — would lock you "
                "out of self-management. Use the dashboard MCP page instead."
            ),
            "status": 423,  # Locked
        }
    return None


# ── Path A: catalog ────────────────────────────────────────────────────


def mcp_list_servers(*, verbose: bool = False) -> dict:
    """List catalog servers. Default returns a compact projection (name,
    transport, is_builtin, attached_agents) — enough to plan attach/detach
    decisions. Pass verbose=True or call mcp_get_server(name) for command/
    args/env. Compact mode is ~85% smaller and unblocks long agent loops
    where the full catalog would otherwise dominate context.
    """
    items = mcp_catalog.list_all(mask_secrets=True)
    if verbose:
        for s in items:
            s["attached_agents"] = mcp_attachments.list_for_server(s["name"])
        return {"servers": items}
    compact = []
    for s in items:
        compact.append({
            "name": s["name"],
            "transport": s["transport"],
            "is_builtin": s["is_builtin"],
            "attached_agents": mcp_attachments.list_for_server(s["name"]),
        })
    return {"servers": compact, "_hint": "use verbose=True or mcp_get_server(name) for command/args/env"}


def mcp_get_server(*, name: str) -> dict:
    s = mcp_catalog.get(name, mask_secrets=True)
    if not s:
        return {"error": f"server {name!r} not found", "status": 404}
    s["attached_agents"] = mcp_attachments.list_for_server(name)
    return s


async def mcp_create_server(
    *, name: str, transport: str,
    command: str | None = None, args: list[str] | None = None,
    env: dict[str, str] | None = None, url: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Path A: install an existing MCP server (config-only). Smoke-test runs
    inside ``mcp_catalog.create``."""
    payload: dict[str, Any] = {"transport": transport}
    if command is not None: payload["command"] = command
    if args is not None: payload["args"] = args
    if env is not None: payload["env"] = env
    if url is not None: payload["url"] = url
    if cwd is not None: payload["cwd"] = cwd
    try:
        return await mcp_catalog.create(name, payload, actor="jarvis")
    except (ValueError, RuntimeError) as exc:
        return {"error": str(exc), "status": 400}


async def mcp_update_server(*, name: str, patch: dict[str, Any]) -> dict:
    block = _self_lockout(name, "updating")
    if block:
        return block
    try:
        result = await mcp_catalog.update(name, patch, actor="jarvis")
    except LookupError as exc:
        return {"error": str(exc), "status": 404}
    except (ValueError, RuntimeError) as exc:
        return {"error": str(exc), "status": 400}
    fanout = await mcp_attachments.reconnect_all_for_server(name, actor="jarvis")
    return {**result, "fanout": fanout}


async def mcp_delete_server(*, name: str) -> dict:
    """Built-in protected. Cascade detaches first."""
    block = _self_lockout(name, "deleting")
    if block:
        return block
    detached = await mcp_attachments.detach_from_all(name, actor="jarvis")
    try:
        result = await mcp_catalog.delete(name, actor="jarvis")
    except PermissionError as exc:
        return {"error": str(exc), "status": 403}
    except LookupError as exc:
        return {"error": str(exc), "status": 404}
    result["detached_from"] = [r.get("agent") for r in detached if r.get("agent")]
    return result


_TOOL_DESC_CHARS = 240  # cap per-tool description in agent-facing payloads


async def mcp_test_server(*, name: str) -> dict:
    """Smoke test + refresh tools cache (mirror of POST /api/mcp/servers/<n>/test).

    Refreshes the cache with full tool descriptions, but trims them to
    _TOOL_DESC_CHARS in the agent-visible payload so a server with many
    verbose tools doesn't dominate context.
    """
    server = mcp_catalog.get(name, mask_secrets=False)
    if not server:
        return {"error": f"server {name!r} not found", "status": 404}
    result = await mcp_catalog.smoke_test(server, return_tool_details=True)
    if result.get("ok") and result.get("tool_details"):
        try:
            from services import shared_state
            if shared_state.registry_db:
                shared_state.registry_db.upsert_server_tools(name, result["tool_details"])
        except Exception:
            logger.exception("[mcp_rpc] tools cache refresh failed for %s", name)
        result["tool_details"] = [
            _trim_tool_detail(t) for t in result["tool_details"]
        ]
    return result


def _trim_tool_detail(t: dict) -> dict:
    desc = (t.get("description") or "").strip()
    if len(desc) > _TOOL_DESC_CHARS:
        desc = desc[:_TOOL_DESC_CHARS].rstrip() + "…"
    return {"name": t.get("name"), "description": desc}


async def mcp_attach_to_agent(*, server: str, agent: str) -> dict:
    try:
        return await mcp_attachments.attach(agent, server, actor="jarvis")
    except LookupError as exc:
        return {"error": str(exc), "status": 404}


async def mcp_detach_from_agent(*, server: str, agent: str) -> dict:
    block = _self_lockout(server, "detaching")
    if block:
        return block
    return await mcp_attachments.detach(agent, server, actor="jarvis")


# ── Path B: env probe + safety ─────────────────────────────────────────


def mcp_check_environment() -> dict:
    return admin.check_environment()


def mcp_recommended_packages() -> dict:
    return admin.recommended_packages()


async def mcp_check_package_safety(*, package_name: str, ecosystem: str = "python") -> dict:
    return await admin.check_package_safety(package_name, ecosystem)


# ── Path B: build pipeline ─────────────────────────────────────────────


def mcp_scaffold_server(
    *, name: str, description: str, planned_tools: list[dict[str, Any]] | None = None,
) -> dict:
    try:
        return admin.scaffold(name, description, planned_tools or [], actor="jarvis")
    except (ValueError, FileExistsError) as exc:
        return {"error": str(exc), "status": 400}


async def mcp_static_check(*, name: str) -> dict:
    try:
        return await admin.static_check(name)
    except LookupError as exc:
        return {"error": str(exc), "status": 404}


async def mcp_install_dependencies(*, name: str) -> dict:
    try:
        return await admin.install_dependencies(name)
    except LookupError as exc:
        return {"error": str(exc), "status": 404}


async def mcp_run_smoke_test(*, name: str) -> dict:
    try:
        return await admin.run_smoke_test(name)
    except LookupError as exc:
        return {"error": str(exc), "status": 404}


async def mcp_run_tool_test(
    *, name: str, tool_name: str,
    args: dict[str, Any] | None = None,
    assertions: list[dict[str, Any]] | None = None,
) -> dict:
    try:
        return await admin.run_tool_test(name, tool_name, args or {}, assertions or [])
    except LookupError as exc:
        return {"error": str(exc), "status": 404}


async def mcp_run_test_suite(*, name: str) -> dict:
    try:
        return await admin.run_test_suite(name)
    except LookupError as exc:
        return {"error": str(exc), "status": 404}


def mcp_verify(*, name: str) -> dict:
    try:
        return admin.verify(name)
    except LookupError as exc:
        return {"error": str(exc), "status": 404}


async def mcp_promote(
    *, name: str, attach_to: list[str] | None = None,
) -> dict:
    try:
        return await admin.promote(name, attach_to=attach_to, actor="jarvis")
    except LookupError as exc:
        return {"error": str(exc), "status": 404}
    except (ValueError, RuntimeError) as exc:
        return {"error": str(exc), "status": 400}


async def mcp_patch_tool(*, name: str, tool_name: str, new_code: str) -> dict:
    try:
        return await admin.patch_tool(name, tool_name, new_code, actor="jarvis")
    except LookupError as exc:
        return {"error": str(exc), "status": 404}
    except ValueError as exc:
        return {"error": str(exc), "status": 400}


# ── Path B: workspace introspection / cleanup ─────────────────────────


def mcp_list_generated() -> dict:
    """Compact projection: planned_tools shrinks to just tool names. Call
    mcp_get_generated(name) for full per-tool args+description."""
    rows = admin.list_generated()
    for r in rows:
        pt = r.get("planned_tools") or []
        r["planned_tools"] = [t.get("name") for t in pt if isinstance(t, dict)]
    return {"generated": rows}


def mcp_get_generated(*, name: str) -> dict:
    try:
        return admin.get_generated(name)
    except LookupError as exc:
        return {"error": str(exc), "status": 404}


def mcp_clean_workspace(*, scope: str = "test_runs") -> dict:
    try:
        return admin.clean_workspace(scope)
    except (ValueError, LookupError) as exc:
        return {"error": str(exc), "status": 400}
    except PermissionError as exc:
        return {"error": str(exc), "status": 403}


# ── Registration ──────────────────────────────────────────────────────


_METHODS: dict[str, Any] = {
    # Path A — catalog
    "mcp.list_servers": mcp_list_servers,
    "mcp.get_server": mcp_get_server,
    "mcp.create_server": mcp_create_server,
    "mcp.update_server": mcp_update_server,
    "mcp.delete_server": mcp_delete_server,
    "mcp.test_server": mcp_test_server,
    "mcp.attach_to_agent": mcp_attach_to_agent,
    "mcp.detach_from_agent": mcp_detach_from_agent,
    # Path B — env + build pipeline
    "mcp.check_environment": mcp_check_environment,
    "mcp.recommended_packages": mcp_recommended_packages,
    "mcp.check_package_safety": mcp_check_package_safety,
    "mcp.scaffold_server": mcp_scaffold_server,
    "mcp.static_check": mcp_static_check,
    "mcp.install_dependencies": mcp_install_dependencies,
    "mcp.run_smoke_test": mcp_run_smoke_test,
    "mcp.run_tool_test": mcp_run_tool_test,
    "mcp.run_test_suite": mcp_run_test_suite,
    "mcp.verify": mcp_verify,
    "mcp.promote": mcp_promote,
    "mcp.patch_tool": mcp_patch_tool,
    "mcp.list_generated": mcp_list_generated,
    "mcp.get_generated": mcp_get_generated,
    "mcp.clean_workspace": mcp_clean_workspace,
}


def register(server: RuntimeRpcServer) -> None:
    """Wire every MCP-admin handler onto the given RPC server. Call once at
    boot from server.py (next to ``skill_rpc_handlers.register``).
    """
    for name, handler in _METHODS.items():
        server.register(name, handler)
    logger.info("[mcp_rpc] registered %d MCP-admin methods", len(_METHODS))
