"""Runtime replacements receive fresh controls, and hook order stays intact."""

from unittest.mock import AsyncMock

import pytest
from fast_agent.agents.agent_types import AgentConfig
from fast_agent.agents.mcp_agent import McpAgent
from fast_agent.agents.tool_runner import ToolRunnerHooks
from fast_agent.context import Context

from services.plugins.live_runtime import register_runtime


def agent():
    context = Context()
    context.no_shell = True
    return McpAgent(AgentConfig(name="Live", servers=[], skills=[]), context=context)


@pytest.mark.asyncio
async def test_register_once_preserves_host_hooks_and_initial_tool_snapshot():
    runtime = agent()
    existing = AsyncMock()
    runtime.tool_runner_hooks = ToolRunnerHooks(before_llm_call=existing)
    first = register_runtime(runtime)
    hooks = runtime.tool_runner_hooks
    assert register_runtime(runtime) is first
    assert runtime.tool_runner_hooks is hooks
    from types import SimpleNamespace

    runner = SimpleNamespace(refresh_tools=AsyncMock())
    await hooks.before_llm_call(runner, [])
    existing.assert_awaited_once_with(runner, [])
    runner.refresh_tools.assert_not_called()
    await hooks.after_turn_complete(runner, None)


def test_replaced_agent_gets_independent_control():
    first, replacement = agent(), agent()
    assert register_runtime(first) is not register_runtime(replacement)


@pytest.mark.asyncio
async def test_reviewed_plugin_is_readable_without_expanding_team_filesystem(tmp_path):
    from fast_agent.agents.agent_types import AgentConfig
    from fast_agent.agents.mcp_agent import McpAgent
    from fast_agent.context import Context
    from mcp.types import CallToolResult, TextContent, Tool

    from services.plugins.package import inspect_package
    from services.plugins.runtime_adapter import apply_to_agent

    class RestrictedFilesystem:
        tools = (
            Tool(
                name="read_text_file",
                description="Workspace only",
                inputSchema={"type": "object"},
            ),
        )

        async def call_tool(
            self, name, arguments=None, tool_use_id=None, *, request_params=None
        ):
            return CallToolResult(
                isError=True, content=[TextContent(type="text", text="Access denied")]
            )

        def metadata(self):
            return {}

    root = tmp_path / "package"
    (root / "skills/review").mkdir(parents=True)
    (root / "plugin.json").write_text('{"name":"reviewed"}')
    path = root / "skills/review/SKILL.md"
    path.write_text("---\nname: review\ndescription: Review\n---\nAPPROVED_PLUGIN_BODY")
    agent = McpAgent(
        AgentConfig(
            name="Team", instruction="Assist. {{agentSkills}}", servers=[], skills=[]
        ),
        context=Context(no_shell=True),
    )
    agent.set_filesystem_runtime(RestrictedFilesystem())
    try:
        assert await apply_to_agent(agent, root, inspect_package(root), None)
        assert agent.skill_read_tool_name == "read_skill"
        assert "read_skill" in [tool.name for tool in (await agent.list_tools()).tools]
        result = await agent.call_tool("read_skill", {"path": str(path)})
        assert not result.isError and "APPROVED_PLUGIN_BODY" in result.content[0].text
        denied = await agent.call_tool(
            "read_skill", {"path": str(tmp_path / "outside-secret")}
        )
        assert denied.isError
        assert not agent.shell_runtime_enabled
    finally:
        await agent.shutdown()
