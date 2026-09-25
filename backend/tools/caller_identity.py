"""Resolve an MCP caller from transport metadata, never from tool arguments."""
from __future__ import annotations

from mcp.server.fastmcp import Context


def caller_agent(ctx: Context | None) -> str:
    """Return the trusted fast-agent caller name, or empty on missing metadata."""
    if ctx is None:
        return ""
    try:
        meta = ctx.request_context.meta
    except (AttributeError, RuntimeError):
        return ""
    if isinstance(meta, dict):
        value = meta.get("caller_agent")
    else:
        value = getattr(meta, "caller_agent", None)
        if value is None:
            extra = getattr(meta, "model_extra", None) or getattr(
                meta, "__pydantic_extra__", None
            )
            value = extra.get("caller_agent") if isinstance(extra, dict) else None
    return value.strip() if isinstance(value, str) else ""
