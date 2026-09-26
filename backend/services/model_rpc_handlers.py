"""RPC adapter for model changes requested by a team agent."""
from __future__ import annotations

import time

from core.agent_registry_db import AgentRegistryDB
from services.activity_stream import activity_stream_manager
from services.team_model_runtime import change_model, snapshot


def _resolve_run(caller_agent: str, session_id: str, target_agent: str = "") -> str:
    if not caller_agent or not session_id or "${" in session_id:
        raise PermissionError("missing bound agent identity or team session")
    name = target_agent or caller_agent
    records = [
        rec for rec in AgentRegistryDB().find_by_name(name)
        if rec.get("session_id") == session_id
    ]
    if not records:
        raise ValueError("target agent has no run in this team session")
    return records[0]["run_id"]


def _get(caller_agent: str, session_id: str, target_agent: str = "") -> dict:
    run_id = _resolve_run(caller_agent, session_id, target_agent)
    return snapshot(run_id, caller_agent=caller_agent)


def _set(
    model_id: str, expected_revision: int, caller_agent: str,
    session_id: str, target_agent: str = "",
) -> dict:
    run_id = _resolve_run(caller_agent, session_id, target_agent)
    result = change_model(
        run_id, model_id, expected_revision,
        actor=f"agent:{caller_agent}", caller_agent=caller_agent,
    )
    if result["changed"]:
        activity_stream_manager.broadcast({
            "agent_name": result["target_agent"],
            "event_type": "model_changed",
            "data": result,
            "timestamp": time.time(),
        })
    return result


def register(server) -> None:
    server.register("model.get", _get)
    server.register("model.set", _set)
