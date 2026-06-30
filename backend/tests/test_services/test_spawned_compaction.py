"""Spawned agents self-compact too (parity with in-process).

isolated_runner now calls ``attach_compaction_hooks_to_all`` on the spawned agent
after installing the spawn hook chain, so a long-running team/dynamic agent compacts
its own message_history instead of growing it unbounded (only in-process agents did
before). These tests pin the merge behaviour the spawned path relies on.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import services.context_compaction as cc
from fast_agent.agents.tool_runner import ToolRunnerHooks


async def test_compaction_merges_onto_spawned_hook_chain(monkeypatch):
    """A spawned agent already carries the spawn hook chain (pause/spawn/rtac). The
    compaction attach MERGES in: both the pre-existing before_llm hook AND the
    proactive compaction run, and the attach is idempotent."""
    ran: list[str] = []

    async def existing_before_llm(_r, _m):
        ran.append("spawn")

    async def fake_compact(*_a, **_k):
        ran.append("compact")

    monkeypatch.setattr(cc, "maybe_compact_agent", fake_compact)

    agent = SimpleNamespace(
        name="DevAgent",
        tool_runner_hooks=ToolRunnerHooks(before_llm_call=existing_before_llm),
    )
    app = MagicMock(_agents={"DevAgent": agent})

    assert cc.attach_compaction_hooks_to_all(app) == 1
    assert getattr(agent, "_jarvis_compaction_hook", False) is True

    runner = SimpleNamespace(_agent=SimpleNamespace(name="DevAgent", message_history=[]))
    await agent.tool_runner_hooks.before_llm_call(runner, [])
    assert ran == ["spawn", "compact"]            # spawn first, compaction merged after

    assert cc.attach_compaction_hooks_to_all(app) == 0   # idempotent (sentinel)


async def test_compaction_on_overflow_preserved_through_merge(monkeypatch):
    """merge_hooks OR-merges on_context_overflow, so a spawned agent keeps the
    emergency-compaction handler too (not just proactive before_llm)."""
    async def existing_before_llm(_r, _m):
        return None

    overflow_calls: list[str] = []

    async def fake_compact(*_a, **_k):
        overflow_calls.append("compact")

    monkeypatch.setattr(cc, "maybe_compact_agent", fake_compact)

    agent = SimpleNamespace(
        name="QA", tool_runner_hooks=ToolRunnerHooks(before_llm_call=existing_before_llm))
    app = MagicMock(_agents={"QA": agent})
    cc.attach_compaction_hooks_to_all(app)

    hooks = agent.tool_runner_hooks
    assert hooks.on_context_overflow is not None      # emergency handler survived the merge
