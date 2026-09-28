"""Correlate newly spawned teams with the chat or voice conversation."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fast_agent.agents.tool_runner import ToolRunnerHooks

from services.activity_stream import activity_stream_manager
from services.team_work_service import bind_team

logger = logging.getLogger(__name__)


def create_team_binding_hooks(conversation_id: str) -> ToolRunnerHooks:
    """Observe spawn tool results without changing fast-agent tool behavior."""
    spawn_call_ids: set[str] = set()

    async def before_tool_call(_runner, message) -> None:
        for call_id, call in (message.tool_calls or {}).items():
            params = getattr(call, "params", None)
            name = str(getattr(params, "name", ""))
            if name.rsplit("__", 1)[-1] == "spawn_team_tool":
                spawn_call_ids.add(call_id)

    async def after_tool_call(_runner, message) -> None:
        for call_id, result in (message.tool_results or {}).items():
            if call_id not in spawn_call_ids:
                continue
            spawn_call_ids.discard(call_id)
            if getattr(result, "isError", False):
                continue
            for block in result.content or []:
                raw = getattr(block, "text", "")
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                session_id = payload.get("session_id") if isinstance(payload, dict) else None
                if not session_id:
                    continue
                try:
                    await asyncio.to_thread(bind_team, str(session_id), conversation_id)
                except Exception:
                    logger.exception("[TEAM-WORK] Could not bind team %s", session_id)
                    activity_stream_manager.broadcast({
                        "event_type": "team_binding_error",
                        "agent_name": "Jarvis",
                        "session_id": session_id,
                        "timestamp": time.time(),
                    })
                break

    return ToolRunnerHooks(
        before_tool_call=before_tool_call,
        after_tool_call=after_tool_call,
    )
