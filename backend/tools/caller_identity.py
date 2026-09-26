"""Resolve the fast-agent stamped caller identity from MCP transport metadata."""
from __future__ import annotations

from mcp.server.fastmcp import Context


def caller_from_ctx(ctx: Context | None) -> str:
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
            extra = getattr(meta, "model_extra", None) or getattr(meta, "__pydantic_extra__", None)
            value = extra.get("caller_agent") if isinstance(extra, dict) else None
    return value.strip() if isinstance(value, str) else ""
