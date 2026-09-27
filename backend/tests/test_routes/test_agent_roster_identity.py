"""The monitor roster must preserve distinct team-session identities."""

from unittest.mock import patch

import pytest


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
