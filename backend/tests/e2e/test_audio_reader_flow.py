"""E2E regression test: audio reader flow against the REAL story_server tool.

What this guards:
 * `find_story_chapter` keeps returning the `response` + `source` JSON shape
   AudioReaderAgent depends on (the [[[READ_LOCAL:title|file]]] tag parsed by
   routes/chat.py + routes/ws_voice.py via check_pending_read / _process_response_tags).
 * Local-priority search (data/stories/{title}/{NNN}_{...}.txt) keeps working.
 * AudioReaderAgent still calls find_story_chapter with (story_name, chapter_number)
   — if any code change alters the LLM-facing tool contract, this fails.

If any of those contracts change, this test fails loud with a clear diff.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.harness import ToolCallRecorder, build_scripted_agent, first_tool_result_text


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def stubbed_stories_dir(tmp_path, monkeypatch):
    """Point story_server.DATA_DIR at a temp dir seeded with one story chapter."""
    data_dir = tmp_path / "data"
    stories_dir = data_dir / "stories" / "Tây Du Ký"
    stories_dir.mkdir(parents=True)
    (stories_dir / "003_Tây Du Ký.txt").write_text("chapter 3 text")

    import tools.story_server as story_server

    monkeypatch.setattr(story_server, "DATA_DIR", str(data_dir))
    return data_dir


@pytest.mark.asyncio
async def test_audio_reader_finds_local_chapter(stubbed_stories_dir):
    """Real find_story_chapter + scripted LLM → expected tool contract."""
    from tools.story_server import find_story_chapter

    recorder = ToolCallRecorder()
    agent = await build_scripted_agent(
        fixture_path=FIXTURES / "audio_reader_single_chapter.yaml",
        tools=[find_story_chapter],
        agent_name="AudioReaderAgent",
        recorder=recorder,
    )

    final = await agent.generate("đọc cho tôi Tây Du Ký chương 3")

    recorder.assert_matches(
        [
            ("find_story_chapter", {"story_name": "Tây Du Ký", "chapter_number": 3}),
        ]
    )

    # The fixture scripts a final assistant turn; what matters for regression is
    # the TOOL's real output, which ToolRunner feeds into the final turn's history.
    history = agent.message_history
    tool_result_messages = [
        msg for msg in history if msg.tool_results
    ]
    assert tool_result_messages, "expected a tool_results message in history"

    raw = first_tool_result_text(tool_result_messages[-1])
    payload = json.loads(raw)

    assert payload["source"] == "local", (
        f"find_story_chapter source shifted away from 'local' priority: {payload}"
    )
    assert "response" in payload, (
        f"find_story_chapter contract broken: missing 'response' field ({payload})"
    )
    assert "Tây Du Ký" in payload["response"]
    assert "chapter 3" in payload["response"]

    # Final assistant reply is the fixture's scripted echo
    assert "[[[READ_LOCAL:" in final.last_text()


@pytest.mark.asyncio
async def test_scripted_llm_exhaustion_fails_loud(stubbed_stories_dir):
    """If the fixture has fewer turns than the agent needs, surface an error —
    never silently continue with filler content."""
    from tests.e2e.scripted_llm import ScriptedLLM
    from tools.story_server import find_story_chapter

    llm = ScriptedLLM(
        turns=[
            {
                "tool_calls": {
                    "c1": {
                        "name": "find_story_chapter",
                        "arguments": {"story_name": "Tây Du Ký", "chapter_number": 3},
                    }
                }
            },
        ]
    )

    from fast_agent.agents.agent_types import AgentConfig
    from fast_agent.agents.tool_agent import ToolAgent

    agent = ToolAgent(
        config=AgentConfig(
            name="exhaustion_test",
            instruction="test",
            servers=[],
            human_input=False,
        ),
        tools=[find_story_chapter],
        context=None,
    )
    agent._llm = llm

    with pytest.raises(RuntimeError, match="ScriptedLLM exhausted"):
        await agent.generate("trigger tool then run out")
