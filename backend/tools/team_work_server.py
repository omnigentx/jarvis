"""Small agent-facing surface for coordinating independent teams."""
from __future__ import annotations

import sys
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.caller_identity import caller_agent  # noqa: E402
from tools.runtime_rpc_client import RuntimeRpcError, call as rpc_call  # noqa: E402

mcp = FastMCP("TeamWork")


def _call(method: str, ctx: Context, params: dict) -> dict:
    caller = caller_agent(ctx)
    if not caller:
        return {"error": "Missing bound agent identity", "status": 403}
    try:
        return rpc_call(method, {"caller_agent": caller, **params})
    except RuntimeRpcError as exc:
        return {"error": f"Team control unavailable: {exc}", "status": 503}


@mcp.tool()
def team_find(query: str = "", limit: int = 20, ctx: Context = None) -> dict:
    """Find teams by name or brief. Returns compact IDs, descriptions and revisions."""
    return _call("team_work.find", ctx, {"query": query, "limit": limit})


@mcp.tool()
def team_revisions(
    session_id: str, limit: int = 20, before_revision: int | None = None,
    ctx: Context = None,
) -> dict:
    """Read the latest changes; use before_revision for older pages."""
    return _call("team_work.revisions", ctx, {
        "session_id": session_id, "limit": limit,
        "before_revision": before_revision,
    })


@mcp.tool()
def team_change_requirement(
    session_id: str,
    change_text: str,
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
    source_refs: list[str] | None = None,
    ctx: Context = None,
) -> dict:
    """Record one user change, assign a revision and deliver it to the team's PM.

    Use the session ID from team_find. If several teams match, ask the user
    which team they mean before calling this tool. A repeated call with the
    same idempotency key returns the original revision.
    """
    return _call("team_work.change", ctx, {
        "session_id": session_id, "change_text": change_text,
        "expected_revision": expected_revision,
        "idempotency_key": idempotency_key, "source_refs": source_refs,
    })


@mcp.tool()
def team_retry_pending(session_id: str, ctx: Context = None) -> dict:
    """Retry this team's pending deliveries after a transport failure."""
    return _call("team_work.retry", ctx, {"session_id": session_id})


if __name__ == "__main__":
    mcp.run(transport="stdio")
