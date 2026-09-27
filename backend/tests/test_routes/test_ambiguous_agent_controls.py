"""Name-only controls must never affect a different same-name team member."""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["pause_agent", "resume_agent", "delete_agent"])
async def test_ambiguous_name_control_fails_before_side_effect(monkeypatch, control):
    from routes import agents
    from services import agent_definitions, shared_state
    from services.pause_controller import pause_controller
    from services.pause_manager import pause_manager

    registry = MagicMock()
    registry.find_by_name.return_value = [
        {"run_id": "one", "session_id": "team-a"},
        {"run_id": "two", "session_id": "team-b"},
    ]
    monkeypatch.setattr(shared_state, "registry_db", registry)
    monkeypatch.setattr(agents, "_is_static_agent", lambda _name: False)
    delete_definition = MagicMock(return_value=False)
    monkeypatch.setattr(agent_definitions, "delete_definition", delete_definition)
    pause = MagicMock(return_value=True)
    resume = MagicMock(return_value=True)
    monkeypatch.setattr(pause_manager, "pause", pause)
    monkeypatch.setattr(pause_controller, "resume", resume)

    with pytest.raises(HTTPException) as error:
        await getattr(agents, control)("Alex [Dev]")
    assert error.value.status_code == 409
    registry.delete_by_name.assert_not_called()
    delete_definition.assert_not_called()
    pause.assert_not_called()
    resume.assert_not_called()


@pytest.mark.asyncio
async def test_same_team_multiple_runs_remains_pauseable(monkeypatch):
    from routes import agents
    from services import shared_state
    from services.pause_manager import pause_manager

    registry = MagicMock()
    registry.find_by_name.return_value = [
        {"run_id": "old", "session_id": "team-a"},
        {"run_id": "new", "session_id": "team-a"},
    ]
    monkeypatch.setattr(shared_state, "registry_db", registry)
    pause = MagicMock(return_value=True)
    monkeypatch.setattr(pause_manager, "pause", pause)

    result = await agents.pause_agent("Alex [Dev]")
    assert result["status"] == "paused"
    pause.assert_called_once_with("Alex [Dev]")


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["member_name", "team_name"])
async def test_team_delete_rejects_collisions_before_cleanup(monkeypatch, collision):
    from routes import agents
    from services import shared_state

    registry = MagicMock()
    other_team = "different-team" if collision == "member_name" else "target-team"
    registry.get_all.return_value = {
        "run-a": {
            "run_id": "run-a", "agent_name": "Alex [Dev]",
            "team_name": "target-team", "session_id": "session-a",
        },
        "run-b": {
            "run_id": "run-b", "agent_name": "Alex [Dev]",
            "team_name": other_team, "session_id": "session-b",
        },
    }
    monkeypatch.setattr(shared_state, "registry_db", registry)

    with pytest.raises(HTTPException) as error:
        await agents.delete_team("target-team")
    assert error.value.status_code == 409
    registry.delete_by_team.assert_not_called()
    registry.delete_by_name.assert_not_called()
