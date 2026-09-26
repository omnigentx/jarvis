"""Team-agent MCP tools for reading and changing live model selection."""
from __future__ import annotations

import sys
import os
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.caller_identity import caller_from_ctx  # noqa: E402
from tools.runtime_rpc_client import RuntimeRpcError, call as rpc_call  # noqa: E402

mcp = FastMCP("ModelSelection")


@mcp.tool()
def model_get(target_agent: str = "", ctx: Context = None) -> dict:
    """Read your model/revision; PM may name another member in this team."""
    caller = caller_from_ctx(ctx)
    if not caller:
        return {"error": "missing caller identity"}
    try:
        return rpc_call("model.get", {
            "target_agent": target_agent, "caller_agent": caller,
            "session_id": os.environ.get("TEAM_SESSION_ID", ""),
        })
    except RuntimeRpcError as exc:
        return {"error": str(exc)}


@mcp.tool()
def model_set(model_id: str, expected_revision: int, target_agent: str = "", ctx: Context = None) -> dict:
    """Change a team agent's model from its NEXT LLM call.

    Pass the revision returned by model_get. Omit target_agent for self. Only the team
    orchestrator may change another member; each member may change itself.
    Currently supports the configured OpenAI-compatible provider only.
    """
    caller = caller_from_ctx(ctx)
    if not caller:
        return {"error": "missing caller identity"}
    try:
        return rpc_call("model.set", {
            "target_agent": target_agent, "model_id": model_id,
            "expected_revision": expected_revision, "caller_agent": caller,
            "session_id": os.environ.get("TEAM_SESSION_ID", ""),
        })
    except RuntimeRpcError as exc:
        return {"error": str(exc)}


if __name__ == "__main__":
    mcp.run()
