"""Bind a spawned team to the conversation that made the tool call."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services.team_work_hooks import create_team_binding_hooks


@pytest.mark.asyncio
async def test_spawn_result_binds_only_its_own_call_id():
    hooks = create_team_binding_hooks("chat-1")
    before = SimpleNamespace(tool_calls={
        "spawn-1": SimpleNamespace(params=SimpleNamespace(
            name="agent_spawner__spawn_team_tool"
        )),
        "search-1": SimpleNamespace(params=SimpleNamespace(
            name="serpapi__search"
        )),
    })
    after = SimpleNamespace(tool_results={
        "search-1": SimpleNamespace(isError=False, content=[
            SimpleNamespace(text='{"session_id":"wrong"}')
        ]),
        "spawn-1": SimpleNamespace(isError=False, content=[
            SimpleNamespace(text='{"session_id":"team-a","status":"orchestrator_spawned"}')
        ]),
    })
    with patch("services.team_work_hooks.bind_team") as bind:
        await hooks.before_tool_call(None, before)
        await hooks.after_tool_call(None, after)
        await hooks.after_tool_call(None, after)

    bind.assert_called_once_with("team-a", "chat-1")


@pytest.mark.asyncio
async def test_failed_spawn_result_is_not_bound():
    hooks = create_team_binding_hooks("chat-2")
    before = SimpleNamespace(tool_calls={
        "spawn-2": SimpleNamespace(params=SimpleNamespace(name="spawn_team_tool"))
    })
    after = SimpleNamespace(tool_results={
        "spawn-2": SimpleNamespace(isError=True, content=[
            SimpleNamespace(text='{"session_id":"team-b"}')
        ])
    })
    with patch("services.team_work_hooks.bind_team") as bind:
        await hooks.before_tool_call(None, before)
        await hooks.after_tool_call(None, after)
    bind.assert_not_called()
