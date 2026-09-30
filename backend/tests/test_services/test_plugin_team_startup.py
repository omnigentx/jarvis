"""The first LLM call cannot race durable capability restoration."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fast_agent.agents.agent_types import AgentConfig
from fast_agent.agents.mcp_agent import McpAgent
from fast_agent.context import Context

from services.plugins import team_runtime


@pytest.mark.asyncio
async def test_initial_llm_boundary_waits_for_runtime_startup(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def startup(*args):
        started.set()
        await release.wait()

    monkeypatch.setattr(team_runtime, "start_team_runtime", startup)
    agent = McpAgent(
        AgentConfig(name="Child", servers=[], skills=[]), context=Context(no_shell=True)
    )
    team_runtime.attach_team_runtime(
        agent, "startup-test", {"session_id": "s", "agent_name": "Child"}
    )
    await started.wait()
    assert agent.tool_runner_hooks is not None
    runner = SimpleNamespace(refresh_tools=AsyncMock())
    boundary = asyncio.create_task(agent.tool_runner_hooks.before_llm_call(runner, []))
    await asyncio.sleep(0)
    assert not boundary.done()
    release.set()
    await asyncio.wait_for(boundary, 2)
    await agent.tool_runner_hooks.after_turn_complete(runner, None)


def test_synchronous_registration_defers_startup_until_first_boundary(monkeypatch):
    startup = AsyncMock()
    monkeypatch.setattr(team_runtime, "start_team_runtime", startup)
    agent = McpAgent(
        AgentConfig(name="SynchronousChild", servers=[], skills=[]),
        context=Context(no_shell=True),
    )
    record = {"session_id": "sync-team", "agent_name": "SynchronousChild"}
    team_runtime.attach_team_runtime(agent, "sync-startup", record)
    hooks = agent.tool_runner_hooks
    team_runtime.attach_team_runtime(agent, "sync-startup", record)
    assert agent.tool_runner_hooks is hooks
    startup.assert_not_called()

    async def first_call():
        runner = SimpleNamespace(refresh_tools=AsyncMock())
        await hooks.before_llm_call(runner, [])
        await hooks.after_turn_complete(runner, None)

    asyncio.run(first_call())
    startup.assert_awaited_once()
