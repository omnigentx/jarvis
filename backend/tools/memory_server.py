"""MCP tool server — exposes durable memory search/fetch to an agent.

Trust model (spec §15, §21): the agent's identity is resolved by ONE mechanism —
the trusted transport ``_meta.caller_agent`` that fast-agent stamps on every tool
call (mcp_aggregator._execute_on_server) from the agent's own name. It works the
same whether this subprocess is POOLED across in-process agents or dedicated to a
spawned one (spawned agents are now named with their real identity, not a generic
"child"). Never from a tool argument; NO fallback chain — a missing identity FAILS
the op rather than silently mis-scoping the write into another agent's silo.

Delegates to the live backend via the RuntimeRpcServer Unix socket (same
pattern as tools/mcp_admin_server.py) — no HTTP, no API key, no model loading
in this subprocess.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.runtime_rpc_client import RuntimeRpcError, call as rpc_call  # noqa: E402

logger = logging.getLogger("memory_server")
mcp = FastMCP("Memory")


def _caller_from_ctx(ctx: Context | None) -> str:
    """The calling agent's identity from the trusted ``_meta.caller_agent`` that
    fast-agent stamps on each tool call. Empty when absent (older callers).
    Tolerant of meta being a dict OR a pydantic model (transport-dependent)."""
    if ctx is None:
        return ""
    try:
        meta = ctx.request_context.meta
    except Exception:  # noqa: BLE001 — no request context (e.g. direct call)
        logger.debug("memory tool: no request_context.meta on ctx (%r) — "
                     "transport may not expose it; owner will fall through to env",
                     type(ctx).__name__, exc_info=True)
        return ""
    if meta is None:
        return ""
    val = None
    if isinstance(meta, dict):
        val = meta.get("caller_agent")
    else:
        val = getattr(meta, "caller_agent", None)
        if val is None:
            extra = getattr(meta, "model_extra", None) or getattr(meta, "__pydantic_extra__", None)
            if isinstance(extra, dict):
                val = extra.get("caller_agent")
    return val.strip() if isinstance(val, str) else ""


def _owner(ctx: Context | None = None) -> str:
    """The bound agent identity — ONE source: the per-call ``caller_agent`` that
    fast-agent stamps from the calling agent's own name. Same for in-process
    (pooled subprocess) and spawned (dedicated subprocess) agents, since every
    agent — including spawned ones — is now named with its real identity.

    NEVER from a tool argument. NO fallback chain: if it doesn't resolve we return
    "" and the caller FAILS the op, rather than silently mis-scoping the write
    into another agent's silo.
    """
    return _caller_from_ctx(ctx)


def _bridge_error(exc: Exception) -> dict:
    return {"error": f"memory backend RPC bridge unavailable: {exc}"}


@mcp.tool()
def memory_search(query: str, types: list[str] | None = None,
                  mode: str = "balanced", limit: int = 5, ctx: Context = None) -> dict:
    """Search YOUR durable memory (episodic history, decisions, preferences,
    procedures). Returns {"memories": [{"id", "type", "text"}, ...]} ordered by
    relevance — ``text`` is the content to use; pass ``id`` to memory_fetch for
    the full source. ``types`` optionally restricts to e.g. ["episodic","semantic"].

    QUERY PHRASING (matters — recall is embedding-based): write ``query`` as a
    SHORT, NATURAL question that NAMES the subject, the way a person asks it
    ("what is the user's job?", "the user's cat's name"). Do NOT stuff abstract
    keywords ("profession goal learning skills current user") — a keyword pile
    embeds far from the concrete stored facts and returns nothing. For a BROAD
    need, issue SEVERAL focused searches (one concept each: job, skills,
    certifications) and merge — not one long catch-all query."""
    owner = _owner(ctx)
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
def memory_fetch(evidence_ids: list[str], ctx: Context = None) -> dict:
    """Fetch the full source content for evidence ids returned by
    memory_search (progressive disclosure)."""
    owner = _owner(ctx)
    if not owner:
        return {"error": "no bound agent identity; memory unavailable"}
    try:
        return rpc_call("memory.fetch", {"agent_name": owner, "evidence_ids": evidence_ids})
    except RuntimeRpcError as exc:
        return _bridge_error(exc)


@mcp.tool()
def memory_remember(content: str, memory_type: str = "semantic", pinned: bool = False, ctx: Context = None) -> dict:
    """STORE a durable fact or preference into memory (this SAVES — use
    memory_search to RECALL what is already stored). Call this when the user
    states something worth keeping ("remember that I…", "from now on…", a
    durable personal fact). ``content`` is the fact to store, phrased clearly.
    ``memory_type``: "semantic" (facts about the user/world), "pinned"
    (standing instructions), "procedural" (reusable workflows). It creates a
    candidate that auto-saves or awaits approval per policy."""
    owner = _owner(ctx)
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
def memory_forget(memory_id: str, reason: str = "", ctx: Context = None) -> dict:
    """Archive one of YOUR memories (reversible, audited)."""
    owner = _owner(ctx)
    if not owner:
        return {"error": "no bound agent identity; memory unavailable"}
    try:
        return rpc_call("memory.forget", {"agent_name": owner, "memory_id": memory_id,
                                          "reason": reason})
    except RuntimeRpcError as exc:
        return _bridge_error(exc)


@mcp.tool()
def procedure_propose(title: str, steps: str, ctx: Context = None) -> dict:
    """Propose a reusable procedure/Skill (always requires approval; never
    auto-published)."""
    owner = _owner(ctx)
    if not owner:
        return {"error": "no bound agent identity; memory unavailable"}
    try:
        return rpc_call("memory.procedure_propose", {"agent_name": owner,
                                                     "title": title, "steps": steps})
    except RuntimeRpcError as exc:
        return _bridge_error(exc)


if __name__ == "__main__":
    mcp.run()
