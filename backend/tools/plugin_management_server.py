"""Scoped, human-reviewed runtime plugin tools for Jarvis and team agents."""

from __future__ import annotations
import os
import sys
from pathlib import Path
from mcp.server.fastmcp import Context, FastMCP

sys.path.insert(0, str(Path(__file__).parent.parent))
from tools.caller_identity import caller_from_ctx  # noqa: E402
from tools.runtime_rpc_client import RuntimeRpcError, call as rpc_call  # noqa: E402

mcp = FastMCP("PluginManagement")


def _call(method: str, params: dict, ctx: Context | None, timeout: float = 30) -> dict:
    caller = caller_from_ctx(ctx)
    if not caller:
        return {"error": "Missing bound caller identity", "status": 403}
    try:
        return rpc_call(
            method,
            {
                **params,
                "caller_agent": caller,
                "session_id": os.environ.get("TEAM_SESSION_ID", ""),
            },
            timeout=timeout,
        )
    except (RuntimeRpcError, OSError) as exc:
        return {"error": str(exc), "status": 503}


@mcp.tool()
def plugin_list(ctx: Context = None) -> dict:
    """List compact stored inventory and your binding, never content bodies.

    stored_status is historical, not proof of current availability. Use
    plugin_activate to obtain an actual live Ready ACK before first use.
    """
    return _call("plugin.list", {}, ctx)


@mcp.tool()
def plugin_add(
    repo: str,
    commit: str,
    subdirectory: str = "",
    target_agent: str = "",
    run_id: str | None = None,
    ctx: Context = None,
) -> dict:
    """Add AND activate an exact GitHub owner/repo + 40-char immutable commit/path.

    Omit target_agent for yourself. Only Jarvis or the team's orchestrator may
    select a subordinate; Jarvis must supply run_id for a team target.
    Human source/content review is mandatory. Pending approval is NOT Ready:
    stop and tell the user; retry only after their approval, never poll.
    MCP execution policy/credentials must be configured by the user in the UI.
    Invoke this tool in a SEPARATE batch from ordinary tools. On Ready, skills
    and MCP tools are available in the NEXT model call in this same turn,
    without restart. Read the namespaced skill or call the new tool to prove use.
    """
    return _call(
        "plugin.add",
        {
            "repo": repo,
            "commit": commit,
            "subdirectory": subdirectory,
            "target_agent": target_agent,
            "run_id": run_id,
        },
        ctx,
        250,
    )


@mcp.tool()
def plugin_activate(
    identity: str,
    target_agent: str = "",
    run_id: str | None = None,
    ctx: Context = None,
) -> dict:
    """Activate an installed ID for self or a managed subordinate after human review.

    Invoke in a separate batch from ordinary tools. Pending review is not Ready;
    do not poll. A Ready ACK means capabilities are usable from the NEXT model
    call, including in the same conversation turn. Does not promote globally.
    """
    return _call(
        "plugin.activate",
        {"identity": identity, "target_agent": target_agent, "run_id": run_id},
        ctx,
        100,
    )


if __name__ == "__main__":
    mcp.run()
