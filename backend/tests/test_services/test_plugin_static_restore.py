"""Durable bindings survive an in-process agent replacement without an LLM."""

from unittest.mock import AsyncMock

import pytest
from fast_agent.agents.agent_types import AgentConfig
from fast_agent.agents.mcp_agent import McpAgent
from fast_agent.context import Context
from sqlalchemy import create_engine

from services.plugins.lifecycle import PluginLifecycle
from services.plugins.runtime_adapter import apply_to_agent


@pytest.mark.asyncio
async def test_static_replacement_restores_reviewed_skill_before_first_llm(tmp_path):
    from services.plugins.restoration import restore_bindings

    engine = create_engine("sqlite:///" + str(tmp_path / "jarvis.db"))
    lifecycle = PluginLifecycle(engine, tmp_path / "plugins")
    source = tmp_path / "source"
    (source / "skills/check").mkdir(parents=True)
    (source / "plugin.json").write_text('{"name":"restore"}')
    (source / "skills/check/SKILL.md").write_text(
        "---\nname: check\ndescription: Check\n---\nRESTORED_STATIC_MARKER"
    )
    record = lifecycle.stage(
        source, repo="openai/plugins", commit="a" * 40, subdirectory=""
    )
    first = McpAgent(
        AgentConfig(name="Jarvis", servers=[], skills=[]),
        context=Context(no_shell=True),
    )
    replacement = McpAgent(
        AgentConfig(name="Jarvis", servers=[], skills=[]),
        context=Context(no_shell=True),
    )
    try:
        await lifecycle.activate(
            record["id"],
            "Jarvis",
            approve=AsyncMock(return_value=True),
            apply=lambda root, package, target: apply_to_agent(
                first, root, package, None
            ),
        )
        assert replacement.skill_manifests == []
        await restore_bindings(replacement, "Jarvis", lifecycle)
        assert [skill.name for skill in replacement.skill_manifests] == [
            "restore:check"
        ]
        tools = await replacement.list_tools()
        reader = next(
            tool.name for tool in tools.tools if tool.name.endswith("read_skill")
        )
        result = await replacement.call_tool(
            reader, {"path": str(replacement.skill_manifests[0].path)}
        )
        assert "RESTORED_STATIC_MARKER" in str(result)
    finally:
        await first.shutdown()
        await replacement.shutdown()
        engine.dispose()
