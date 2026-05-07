"""MCP runtime helpers: audit context manager + env placeholder resolution.

The audit context manager is the single instrumentation point for all MCP
catalog/attachment lifecycle mutations. Every CRUD path in mcp_catalog and
mcp_attachments wraps its work in `audit(...)` so we get:

  * One INFO log line per action (start + done) in jarvis.log under "mcp" logger
  * One row in mcp_event_log table (DB-backed audit trail)
  * One activity_stream broadcast event with type="mcp" (realtime SSE)

Failures inside the `async with audit(...)` block flip outcome to "fail",
attach the exception string to the detail JSON, and re-raise so callers can
return appropriate HTTP status.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from core.database import McpEventLogModel, SessionLocal

logger = logging.getLogger("mcp")

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


def resolve_env(raw_env: dict[str, Any]) -> dict[str, str]:
    """Expand ${VAR} placeholders in an env dict against os.environ."""
    result: dict[str, str] = {}
    for k, v in raw_env.items():
        s = str(v) if v is not None else ""
        result[k] = _PLACEHOLDER_RE.sub(lambda m: os.environ.get(m.group(1), ""), s)
    return result


class _AuditState:
    __slots__ = ("detail", "outcome", "error")

    def __init__(self, initial: dict[str, Any] | None) -> None:
        self.detail: dict[str, Any] = dict(initial or {})
        self.outcome: str = "ok"
        self.error: str | None = None

    def set(self, **kwargs: Any) -> None:
        self.detail.update(kwargs)


@asynccontextmanager
async def audit(
    action: str,
    *,
    server: str | None = None,
    agent: str | None = None,
    actor: str = "user",
    detail: dict[str, Any] | None = None,
) -> AsyncIterator[_AuditState]:
    """Wrap an MCP mutation. See module docstring."""
    started = time.time()
    state = _AuditState(detail)
    logger.info(
        "[mcp.%s] start server=%s agent=%s actor=%s", action, server, agent, actor
    )
    try:
        yield state
    except Exception as exc:
        state.outcome = "fail"
        state.error = f"{type(exc).__name__}: {exc}"
        logger.exception("[mcp.%s] FAIL server=%s agent=%s", action, server, agent)
        raise
    finally:
        duration_ms = int((time.time() - started) * 1000)
        merged_detail = dict(state.detail)
        if state.error:
            merged_detail["error"] = state.error

        # DB row
        try:
            with SessionLocal() as db:
                db.add(
                    McpEventLogModel(
                        timestamp=time.time(),
                        action=action,
                        server_name=server,
                        agent_name=agent,
                        actor=actor,
                        outcome=state.outcome,
                        duration_ms=duration_ms,
                        detail_json=json.dumps(merged_detail) if merged_detail else None,
                    )
                )
                db.commit()
        except Exception:
            logger.exception("[mcp.%s] failed to persist audit row", action)

        # Realtime broadcast (best-effort, lazy import to avoid cycles)
        try:
            from services.activity_stream import activity_stream_manager

            activity_stream_manager.broadcast(
                {
                    "type": "mcp",
                    "action": action,
                    "server": server,
                    "agent": agent,
                    "outcome": state.outcome,
                    "duration_ms": duration_ms,
                    "detail": merged_detail,
                    "ts": time.time(),
                }
            )
        except Exception:
            logger.exception("[mcp.%s] failed to broadcast activity event", action)

        logger.info(
            "[mcp.%s] done outcome=%s duration_ms=%d server=%s agent=%s",
            action,
            state.outcome,
            duration_ms,
            server,
            agent,
        )
