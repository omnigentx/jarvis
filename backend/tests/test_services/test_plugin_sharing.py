"""Global sharing is a separate human decision; a saved flag is not live ACK."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine

from services.plugins.lifecycle import PluginLifecycle, PluginStateError


@pytest.fixture
def lifecycle(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "db"))
    lifecycle = PluginLifecycle(engine, tmp_path / "plugins")
    source = tmp_path / "source"
    (source / "skills/check").mkdir(parents=True)
    (source / "plugin.json").write_text('{"name":"shared"}')
    (source / "skills/check/SKILL.md").write_text(
        "---\nname: check\ndescription: Check\n---\nShared marker"
    )
    lifecycle.stage(source, repo="openai/plugins", commit="a" * 40, subdirectory="")
    yield lifecycle
    engine.dispose()


@pytest.mark.asyncio
async def test_global_promotion_requires_distinct_approval_and_actual_ack(lifecycle):
    from services.plugins.sharing import promote

    record = lifecycle.list()[0]
    apply = AsyncMock(return_value=True)
    pending = await promote(
        lifecycle,
        record["id"],
        ["Jarvis"],
        approve=AsyncMock(return_value=False),
        apply=apply,
    )
    assert pending["status"] == "needs_global_approval"
    apply.assert_not_called()
    assert not lifecycle.get(record["id"])["global_enabled"]
    ready = await promote(
        lifecycle,
        record["id"],
        ["Jarvis"],
        approve=AsyncMock(return_value=True),
        apply=apply,
    )
    assert ready["global_enabled"] and ready["bindings"][0]["status"] == "ready"
    assert lifecycle.cleanup(now=10**12) == []
    with pytest.raises(PluginStateError, match="sharing"):
        lifecycle.uninstall(record["id"])


@pytest.mark.asyncio
async def test_partial_global_application_is_not_reported_ready(lifecycle):
    from services.plugins.sharing import promote

    record = lifecycle.list()[0]
    result = await promote(
        lifecycle,
        record["id"],
        ["Jarvis", "Reader"],
        approve=AsyncMock(return_value=True),
        apply=AsyncMock(side_effect=[True, False]),
    )
    assert result["status"] == "activation_failed"
    assert result["global_enabled"]


@pytest.mark.asyncio
async def test_new_agent_inherits_only_globally_reviewed_version(lifecycle):
    from fast_agent.agents.agent_types import AgentConfig
    from fast_agent.agents.mcp_agent import McpAgent
    from fast_agent.context import Context

    from services.plugins.restoration import restore_bindings
    from services.plugins.sharing import promote, stop_sharing

    record = lifecycle.list()[0]
    await promote(
        lifecycle,
        record["id"],
        [],
        approve=AsyncMock(return_value=True),
        apply=AsyncMock(),
    )
    first = McpAgent(
        AgentConfig(name="New", servers=[], skills=[]), context=Context(no_shell=True)
    )
    after = McpAgent(
        AgentConfig(name="Later", servers=[], skills=[]), context=Context(no_shell=True)
    )
    try:
        await restore_bindings(first, "New", lifecycle)
        assert [skill.name for skill in first.skill_manifests] == ["shared:check"]
        assert lifecycle.get(record["id"])["bindings"][0]["status"] == "ready"
        stop_sharing(lifecycle, record["id"])
        await restore_bindings(after, "Later", lifecycle)
        assert after.skill_manifests == []
        assert [skill.name for skill in first.skill_manifests] == ["shared:check"]
    finally:
        await first.shutdown()
        await after.shutdown()


@pytest.mark.asyncio
async def test_stop_sharing_cannot_race_pending_global_review(lifecycle):
    import asyncio

    from services.plugins.sharing import promote, stop_sharing

    entered, release = asyncio.Event(), asyncio.Event()

    async def approve(_record):
        entered.set()
        await release.wait()
        return True

    identity = lifecycle.list()[0]["id"]
    operation = asyncio.create_task(
        promote(lifecycle, identity, [], approve=approve, apply=AsyncMock())
    )
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(PluginStateError, match="sharing operation"):
            stop_sharing(lifecycle, identity)
    finally:
        release.set()
        await operation


@pytest.mark.asyncio
async def test_sharing_changes_publish_flag_without_fake_binding(lifecycle):
    from services.plugins.sharing import promote, stop_sharing

    events = []
    lifecycle.on_status = events.append
    identity = lifecycle.list()[0]["id"]
    await promote(
        lifecycle, identity, [], approve=AsyncMock(return_value=True), apply=AsyncMock()
    )
    assert events[-1]["global_enabled"] is True
    assert events[-1]["agent"] is None
    stop_sharing(lifecycle, identity)
    assert events[-1]["global_enabled"] is False
