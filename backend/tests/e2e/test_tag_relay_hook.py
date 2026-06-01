"""E2E regression: TagRelayAgent's before_llm_call hook injects a SYSTEM
reminder whenever a tool result contains a `[[[TAG:value]]]` pattern.

MEMORY flags this path as fragile:
 * Must use `before_llm_call` (not `after_tool_call`) because
   `_stage_tool_response` resets `_delta_messages` AFTER `after_tool_call`,
   wiping anything appended there.
 * Tags use triple brackets `[[[TAG:value]]]`.

What this guards:
 * Fixture: tool returns a payload whose `response` field contains a tag.
 * Expectation: the NEXT LLM turn's input messages contain the hook-injected
   `SYSTEM: Your response MUST include this exact tag: ...` reminder.
 * If someone moves the hook to after_tool_call, or renames the tag pattern,
   or drops the append_messages call, this test catches it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fast_agent.agents.agent_types import AgentConfig
from fast_agent.agents.tool_agent import ToolAgent

from tests.e2e.harness import flatten_text
from tests.e2e.scripted_llm import ScriptedLLM


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def seeded_stories(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    story_dir = data_dir / "stories" / "Tây Du Ký"
    story_dir.mkdir(parents=True)
    (story_dir / "003_Tây Du Ký.txt").write_text("chapter 3")

    import tools.story_server as story_server
    monkeypatch.setattr(story_server, "DATA_DIR", str(data_dir))
    return data_dir


@pytest.mark.asyncio
async def test_tag_relay_hook_injects_reminder(seeded_stories):
    # Import the real hook from agent.py so we test the production wiring,
    # not a reimplementation.
    from agent import TAG_RELAY_HOOKS
    from tools.story_server import find_story_chapter

    llm = ScriptedLLM.from_yaml(FIXTURES / "tag_relay_hook.yaml")
    agent = ToolAgent(
        config=AgentConfig(
            name="AudioReaderAgent",
            instruction="test",
            servers=[],
            human_input=False,
        ),
        tools=[find_story_chapter],
        context=None,
    )
    agent._llm = llm
    agent.tool_runner_hooks = TAG_RELAY_HOOKS

    await agent.generate("đọc cho tôi Tây Du Ký chương 3")

    # Turn 1: user task only — no tag-reminder yet.
    turn1_messages = llm.observed_messages[0]
    turn1_text = flatten_text(turn1_messages)
    assert "MUST include this exact tag" not in turn1_text, (
        f"Hook fired too early — no tool result yet:\n{turn1_text}"
    )

    # Turn 2: after find_story_chapter returned with a [[[READ_LOCAL:...]]] tag
    # in its response field, the hook must have appended a SYSTEM reminder.
    # (The tool now emits ONE canonical [[[READ_LOCAL]]] tag — the old extra
    # [[[AUDIO_URL]]] was redundant and leaked into the chat bubble, so it was
    # dropped. The hook is tag-agnostic; it relays whatever [[[TAG:value]]] the
    # tool result carries.)
    assert len(llm.observed_messages) >= 2, (
        "Expected at least 2 LLM calls (initial + post-tool); "
        f"got {len(llm.observed_messages)}"
    )
    turn2_text = flatten_text(llm.observed_messages[1])
    assert "MUST include this exact tag" in turn2_text, (
        "TagRelayAgent hook failed to inject reminder into turn 2.\n"
        f"Turn 2 messages:\n{turn2_text}"
    )
    # The actual tag value should be present too — proves the regex extracted
    # the tag from the tool result's `response` field.
    assert "[[[READ_LOCAL:" in turn2_text, (
        "Hook injected reminder but tag value missing — regex may have "
        f"drifted from [[[TAG:value]]] pattern.\nTurn 2:\n{turn2_text}"
    )


@pytest.mark.asyncio
async def test_no_tag_no_reminder(seeded_stories, tmp_path):
    """Control: when the tool result has NO tag, the hook must NOT inject
    a reminder. Catches over-eager false positives."""
    from agent import TAG_RELAY_HOOKS

    def plain_tool(query: str) -> str:
        return '{"result": "just text, no tags here"}'

    # Minimal fixture inline — a simpler single-tool-call flow
    llm = ScriptedLLM(
        turns=[
            {
                "tool_calls": {
                    "c1": {"name": "plain_tool", "arguments": {"query": "x"}}
                }
            },
            {"content": "done"},
        ]
    )
    agent = ToolAgent(
        config=AgentConfig(
            name="test", instruction="test", servers=[], human_input=False
        ),
        tools=[plain_tool],
        context=None,
    )
    agent._llm = llm
    agent.tool_runner_hooks = TAG_RELAY_HOOKS

    await agent.generate("run plain tool")

    for i, msgs in enumerate(llm.observed_messages):
        text = flatten_text(msgs)
        assert "MUST include this exact tag" not in text, (
            f"Hook falsely injected reminder at turn {i} with no tags in input:\n{text}"
        )


