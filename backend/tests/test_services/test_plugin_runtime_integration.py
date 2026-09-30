"""Exercise actual McpAgent and refresh; no mock runtime acknowledgement."""

from types import SimpleNamespace

import pytest
from fast_agent.agents.agent_types import AgentConfig
from fast_agent.agents.mcp_agent import McpAgent
from fast_agent.context import Context

from services.plugins.activation import apply_skills
from services.plugins.package import inspect_package


def package(tmp_path):
    (tmp_path / "plugin.json").write_text('{"name":"review"}')
    skill = tmp_path / "skills/review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review changes\n---\nPLUGIN_BODY_MARKER"
    )
    return inspect_package(tmp_path)


@pytest.mark.asyncio
async def test_live_instruction_and_reader_updated_without_restart(tmp_path):
    context = Context()
    context.no_shell = True
    agent = McpAgent(
        AgentConfig(
            name="Jarvis", instruction="Assist. {{agentSkills}}", servers=[], skills=[]
        ),
        context=context,
    )
    original_id = id(agent)
    assert "review:review" not in agent.instruction
    assert await apply_skills(
        tmp_path,
        package(tmp_path),
        "Jarvis",
        app=SimpleNamespace(get_agent=lambda _: agent),
    )
    assert id(agent) == original_id
    assert "review:review" in agent.instruction
    assert "PLUGIN_BODY_MARKER" not in agent.instruction
    assert agent.shell_runtime_enabled is False
    tools = await agent.list_tools()
    assert "read_skill" in {tool.name for tool in tools.tools}


@pytest.mark.asyncio
async def test_skill_install_cannot_implicitly_grant_shell(tmp_path):
    context = Context()
    agent = McpAgent(
        AgentConfig(
            name="Jarvis", instruction="Assist. {{agentSkills}}", servers=[], skills=[]
        ),
        context=context,
    )
    assert agent.shell_runtime_enabled is False
    assert not await apply_skills(
        tmp_path,
        package(tmp_path),
        "Jarvis",
        app=SimpleNamespace(get_agent=lambda _: agent),
    )
    assert agent.shell_runtime_enabled is False
    assert not agent.skill_manifests
