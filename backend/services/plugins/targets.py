"""Resolve explicit team run selection to a stable session/agent binding."""

from __future__ import annotations

import hashlib
import os
from typing import Any

from services.plugins.lifecycle import PluginStateError


def team_binding(record: dict[str, Any]) -> str:
    session, name = record.get("session_id"), record.get("agent_name")
    if (
        not isinstance(session, str)
        or not session
        or not isinstance(name, str)
        or not name
    ):
        raise PluginStateError("Team runtime has no stable session identity")
    return "team:" + hashlib.sha256((session + "\0" + name).encode()).hexdigest()


def resolve_target(agent_name: str, run_id: str | None, *, remove: bool = False) -> str:
    from services import shared_state

    if run_id:
        record = (
            shared_state.registry_db.get_record(run_id)
            if shared_state.registry_db
            else None
        )
        if not record or record.get("agent_name") != agent_name:
            raise PluginStateError("Selected team runtime does not match target agent")
        if not remove and not runtime_candidate(record):
            raise PluginStateError(
                "Selected team runtime is not live; resume it before activation"
            )
        return team_binding(record)
    if shared_state.agent_app:
        agent = shared_state.agent_app.get_agent(agent_name)
        if agent is not None:
            return agent_name
    raise PluginStateError("Select an explicit live team run or an in-process agent")


def live_team_run(binding: str) -> dict[str, Any]:
    from services import shared_state

    records = shared_state.registry_db.get_all() if shared_state.registry_db else {}
    selected = []
    for record in records.values():
        if not runtime_candidate(record):
            continue
        try:
            if team_binding(record) == binding:
                selected.append(record)
        except PluginStateError:
            continue
    if len(selected) != 1:
        raise PluginStateError("Team target has no unique live runtime")
    return selected[0]


def matching_team_records(binding: str) -> list[dict[str, Any]]:
    from services import shared_state

    records = shared_state.registry_db.get_all() if shared_state.registry_db else {}
    found = []
    for record in records.values():
        try:
            if team_binding(record) == binding:
                found.append(record)
        except PluginStateError:
            continue
    return found


def confirmed_team_processes_absent(binding: str) -> bool:
    records = matching_team_records(binding)
    if not records:
        return False
    for record in records:
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            return False
        return False
    return True


def runtime_candidate(record: dict[str, Any]) -> bool:
    """Ignore confirmed-dead PIDs, never assume that unknown identity is dead."""
    if record.get("status") not in {"running", "pending", "idle", "paused"}:
        return False
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True
