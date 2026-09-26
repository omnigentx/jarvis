"""RPC adapter for model changes requested by a team agent."""
from __future__ import annotations

import time

from core.agent_registry_db import AgentRegistryDB
from services.activity_stream import activity_stream_manager
from services.inprocess_model_runtime import change_inprocess_model, inprocess_snapshot
from services.team_model_runtime import change_model, snapshot


def _inprocess_target(caller_agent: str, target_agent: str) -> tuple[str, str]:
    """Resolve caller and capability from server-owned runtime definitions."""
    from agent import fast
    name = target_agent or caller_agent
    caller = fast.agents.get(caller_agent)
    target = fast.agents.get(name)
    if not caller or not target:
        raise PermissionError("caller or target is not a registered in-process agent")
    config = caller.get("config")
    if "model_selection" not in (getattr(config, "servers", None) or []):
        raise PermissionError("caller lacks model-management capability")
    if caller_agent != name and caller_agent != "Jarvis":
        raise PermissionError("only Jarvis may change another in-process agent")
    base = getattr(target.get("config"), "model", None)
    if not base:
        from services.team_model_runtime import _default_model
        base = _default_model()
    return name, base


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
    if not session_id:
        name, base = _inprocess_target(caller_agent, target_agent)
        return inprocess_snapshot(name, base)
    run_id = _resolve_run(caller_agent, session_id, target_agent)
    return snapshot(run_id, caller_agent=caller_agent)


def _set(
    model_id: str, expected_revision: int, caller_agent: str,
    session_id: str, target_agent: str = "",
) -> dict:
    activity_stream_manager.broadcast({
        "agent_name": caller_agent, "event_type": "model_change_requested",
        "data": {"actor": f"agent:{caller_agent}", "requested_model": model_id},
        "timestamp": time.time(),
    })
    try:
        result = _set_authorized(model_id, expected_revision, caller_agent, session_id, target_agent)
    except (PermissionError, ValueError) as exc:
        activity_stream_manager.broadcast({
            "agent_name": caller_agent, "event_type": "model_change_failed",
            "data": {"actor": f"agent:{caller_agent}", "requested_model": model_id,
                     "error": str(exc)}, "timestamp": time.time(),
        })
        raise
    if result["changed"]:
        activity_stream_manager.broadcast({
            "agent_name": result["target_agent"],
            "event_type": "model_changed",
            "data": result,
            "timestamp": time.time(),
        })
    return result


def _set_authorized(
    model_id: str, expected_revision: int, caller_agent: str,
    session_id: str, target_agent: str,
) -> dict:
    if session_id:
        run_id = _resolve_run(caller_agent, session_id, target_agent)
        result = change_model(
            run_id, model_id, expected_revision,
            actor=f"agent:{caller_agent}", caller_agent=caller_agent,
        )
    else:
        name, base = _inprocess_target(caller_agent, target_agent)
        result = change_inprocess_model(
            name, base, model_id, expected_revision, actor=f"agent:{caller_agent}",
        )
    return result


def register(server) -> None:
    server.register("model.get", _get)
    server.register("model.set", _set)
