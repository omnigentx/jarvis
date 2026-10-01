from unittest.mock import MagicMock, patch
import pytest
from fast_agent.spawn.message_bus import MessageBus


@pytest.mark.asyncio
async def test_startup_recovers_only_latest_unpaused_session_inbox(tmp_path):
    from services.team_inbox_recovery import recover_team_inboxes

    path = tmp_path / "messages"
    bus = MessageBus(path)
    bus.send(from_name="Dashboard", to_name="Dev", content="requirement")

    def row(rid, status, started):
        return {
            "run_id": rid,
            "session_id": "team",
            "agent_name": "Dev",
            "started_at": started,
            "status": status,
            "original_config": {"env_vars": {"TEAM_MESSAGES_DIR": str(path)}},
        }

    reg = MagicMock()
    reg.get_all.return_value = {
        "old": row("old", "idle", 1),
        "new": row("new", "idle", 2),
    }
    with patch(
        "fast_agent.spawn.servers._team_helpers.wake_team_agent",
        return_value="signaled",
    ) as wake:
        await recover_team_inboxes(reg)
    wake.assert_called_once_with("team", "Dev", "new")
    reg.get_all.return_value = {"new": row("new", "paused", 2)}
    with patch("fast_agent.spawn.servers._team_helpers.wake_team_agent") as wake:
        await recover_team_inboxes(reg)
    wake.assert_not_called()


@pytest.mark.asyncio
async def test_inject_wake_failure_keeps_message_without_false_started(tmp_path):
    from routes.inject import _inject_via_message_bus

    rec = {
        "session_id": "team",
        "run_id": "r",
        "original_config": {"env_vars": {"TEAM_MESSAGES_DIR": str(tmp_path)}},
    }
    with (
        patch("routes.inject.activity_stream_manager") as stream,
        patch(
            "fast_agent.spawn.servers._team_helpers.wake_team_agent",
            side_effect=RuntimeError("socket vanished"),
        ),
    ):
        result = await _inject_via_message_bus("Dev", "requirement", rec)
    assert result.wake_status == "failed"
    assert result.message_id
    assert len(MessageBus(tmp_path).read_unread("Dev")) == 1
    assert not any(
        c.args[0]["event_type"] == "started" for c in stream.broadcast.call_args_list
    )
