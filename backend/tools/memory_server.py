"""MCP tool server — exposes durable memory search/fetch to an agent.

Trust model (spec §15, §21): the agent's identity is BOUND at spawn time via
the ``MEMORY_AGENT_NAME`` environment variable (falls back to ``TEAM_MY_NAME``).
The LLM never passes an identity — these tools inject the bound name into the
RPC call, so an agent can only ever reach its OWN memory. Tool arguments that
look like an identity are ignored.

Delegates to the live backend via the RuntimeRpcServer Unix socket (same
pattern as tools/mcp_admin_server.py) — no HTTP, no API key, no model loading
in this subprocess.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.runtime_rpc_client import RuntimeRpcError, call as rpc_call  # noqa: E402

logger = logging.getLogger("memory_server")
mcp = FastMCP("Memory")


def _owner() -> str:
    """The bound agent identity. NEVER taken from a tool argument.

    TEAM_MY_NAME (set per-agent by the spawn env, config_reader.get_server_env)
    takes precedence over MEMORY_AGENT_NAME (the in-process master's static
    config value). This ordering is a SECURITY invariant: a spawned agent
    inherits the static MEMORY_AGENT_NAME=<master> from the config block, so
    its own TEAM_MY_NAME must win or it would read the master's memory.
    """
    return os.environ.get("TEAM_MY_NAME") or os.environ.get("MEMORY_AGENT_NAME") or ""


def _bridge_error(exc: Exception) -> dict:
    return {"error": f"memory backend RPC bridge unavailable: {exc}"}


@mcp.tool()
def memory_search(query: str, types: list[str] | None = None,
                  mode: str = "balanced", limit: int = 5) -> dict:
    """Search YOUR durable memory (episodic history, decisions, preferences,
    procedures). Returns {"memories": [{"id", "type", "text"}, ...]} ordered by
    relevance — ``text`` is the content to use; pass ``id`` to memory_fetch for
    the full source. ``types`` optionally restricts to e.g. ["episodic","semantic"]."""
    owner = _owner()
    if not owner:
        return {"error": "no bound agent identity; memory unavailable"}
    try:
        return rpc_call("memory.search", {
            "agent_name": owner, "query": query,
            "types": types, "mode": mode, "limit": limit,
        })
    except RuntimeRpcError as exc:
        return _bridge_error(exc)


@mcp.tool()
def memory_fetch(evidence_ids: list[str]) -> dict:
    """Fetch the full source content for evidence ids returned by
    memory_search (progressive disclosure)."""
    owner = _owner()
    if not owner:
        return {"error": "no bound agent identity; memory unavailable"}
    try:
        return rpc_call("memory.fetch", {"agent_name": owner, "evidence_ids": evidence_ids})
    except RuntimeRpcError as exc:
        return _bridge_error(exc)


@mcp.tool()
def memory_remember(content: str, memory_type: str = "semantic", pinned: bool = False) -> dict:
    """STORE a durable fact or preference into memory (this SAVES — use
    memory_search to RECALL what is already stored). Call this when the user
    states something worth keeping ("remember that I…", "from now on…", a
    durable personal fact). ``content`` is the fact to store, phrased clearly.
    ``memory_type``: "semantic" (facts about the user/world), "pinned"
    (standing instructions), "procedural" (reusable workflows). It creates a
    candidate that auto-saves or awaits approval per policy."""
    owner = _owner()
    if not owner:
        return {"error": "no bound agent identity; memory unavailable"}
    try:
        return rpc_call("memory.remember", {
            "agent_name": owner, "content": content,
            "memory_type": memory_type, "pinned": pinned,
        })
    except RuntimeRpcError as exc:
        return _bridge_error(exc)


@mcp.tool()
def memory_forget(memory_id: str, reason: str = "") -> dict:
    """Archive one of YOUR memories (reversible, audited)."""
    owner = _owner()
    if not owner:
        return {"error": "no bound agent identity; memory unavailable"}
    try:
        return rpc_call("memory.forget", {"agent_name": owner, "memory_id": memory_id,
                                          "reason": reason})
    except RuntimeRpcError as exc:
        return _bridge_error(exc)


@mcp.tool()
def procedure_propose(title: str, steps: str) -> dict:
    """Propose a reusable procedure/Skill (always requires approval; never
    auto-published)."""
    owner = _owner()
    if not owner:
        return {"error": "no bound agent identity; memory unavailable"}
    try:
        return rpc_call("memory.procedure_propose", {"agent_name": owner,
                                                     "title": title, "steps": steps})
    except RuntimeRpcError as exc:
        return _bridge_error(exc)


if __name__ == "__main__":
    mcp.run()
