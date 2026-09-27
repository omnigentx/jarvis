"""The worker-cycle close must wake, not fork, an idle-labelled live PM."""

from unittest.mock import MagicMock, patch

import pytest

from services.spawn_progress_bridge import SpawnProgressBridge


@pytest.mark.asyncio
@pytest.mark.parametrize("session_field", ["session_id", "original_config"])
async def test_cycle_wake_uses_session_scoped_channel(session_field):
    bridge = SpawnProgressBridge(progress_manager=MagicMock(), registry_db=MagicMock())
    record = {"agent_name": "Bailey [PM]", "run_id": "be83e1b4", "status": "idle"}
    if session_field == "session_id":
        record["session_id"] = "14688e5f"
    else:
        record["original_config"] = {"env_vars": {"TEAM_SESSION_ID": "14688e5f"}}

    with patch(
        "fast_agent.spawn.servers._team_helpers.wake_team_agent",
        return_value="signaled",
    ) as wake, patch("services.inject_resume.resume_with_inject") as spawn:
        await bridge._trigger_orchestrator_resume(record, "team-b")

    wake.assert_called_once_with("14688e5f", "Bailey [PM]", "be83e1b4")
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_cycle_wake_refuses_missing_identity(caplog):
    bridge = SpawnProgressBridge(progress_manager=MagicMock(), registry_db=MagicMock())
    record = {"agent_name": "Bailey [PM]", "run_id": "be83e1b4", "status": "idle"}

    with patch(
        "fast_agent.spawn.servers._team_helpers.wake_team_agent",
        side_effect=ValueError("session_id, agent_name, and expected_run_id are required"),
    ), patch("services.inject_resume.resume_with_inject") as spawn:
        await bridge._trigger_orchestrator_resume(record, "team-b")

    spawn.assert_not_called()
    assert "Failed to wake orchestrator" in caplog.text
