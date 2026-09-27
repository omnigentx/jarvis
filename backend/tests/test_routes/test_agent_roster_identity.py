"""The monitor roster must preserve distinct team-session identities."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_list_agents_keeps_same_name_in_independent_teams():
    from routes.agents import list_agents
    import services.shared_state as state

    records = {
        "run-a": {
            "run_id": "run-a", "agent_name": "Alex [PM]", "role": "pm",
            "team_name": "team-a", "session_id": "session-a", "status": "running",
        },
        "run-b": {
            "run_id": "run-b", "agent_name": "Alex [PM]", "role": "pm",
            "team_name": "team-b", "session_id": "session-b", "status": "idle",
        },
    }

    class Registry:
        def get_all(self):
            return records

    with patch.object(state, "registry_db", Registry()), \
         patch("routes.agents._build_agents_from_runtime", return_value=[]), \
         patch("routes.agents._snapshots_db_path", return_value=None), \
         patch("routes.agents._compute_effective_status", side_effect=lambda r, **_: r["status"]), \
         patch("routes.agents._icon_for_role", return_value="smart_toy"), \
         patch("routes.agents._get_default_model", return_value="openai.test"):
        result = await list_agents()

    assert [(agent["session_id"], agent["run_id"], agent["name"])
            for agent in result] == [
        ("session-a", "run-a", "Alex [PM]"),
        ("session-b", "run-b", "Alex [PM]"),
    ]


@pytest.mark.asyncio
async def test_message_history_requires_session_for_duplicate_names():
    from routes.agents import get_agent_messages, get_agent_turn_full
    import services.shared_state as state

    class Registry:
        def find_by_name(self, name):
            assert name == "Alex [PM]"
            return [{"session_id": "session-a"}, {"session_id": "session-b"}]

    with patch.object(state, "registry_db", Registry()), \
         patch("services.agent_message_stream.list_agent_messages", return_value={"turns": [], "total": 0}) as list_messages, \
         patch("services.agent_message_stream.get_agent_turn_full", return_value={"turn_idx": 0}) as get_full:
        with pytest.raises(HTTPException) as ambiguous:
            await get_agent_messages("Alex [PM]")
        assert ambiguous.value.status_code == 409
        with pytest.raises(HTTPException) as missing:
            await get_agent_turn_full("Alex [PM]", 0, session_id="other")
        assert missing.value.status_code == 404

        await get_agent_messages("Alex [PM]", session_id="session-b")
        await get_agent_turn_full("Alex [PM]", 0, session_id="session-a")
        assert list_messages.call_args.kwargs["session_id"] == "session-b"
        assert get_full.call_args.kwargs["session_id"] == "session-a"


@pytest.mark.asyncio
async def test_unambiguous_name_only_history_uses_its_team_session():
    from routes.agents import get_agent_messages
    import services.shared_state as state

    class Registry:
        def find_by_name(self, _name):
            return [{"session_id": "only-team"}]

    with patch.object(state, "registry_db", Registry()), \
         patch("services.agent_message_stream.list_agent_messages", return_value={"turns": [], "total": 0}) as list_messages:
        await get_agent_messages("Alex [PM]")
    assert list_messages.call_args.kwargs["session_id"] == "only-team"
