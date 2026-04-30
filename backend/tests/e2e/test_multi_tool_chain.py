"""E2E regression: multi-tool chain through ToolRunner.

Proves that an agent can call Tool A, see its result, then issue Tool B with
args influenced by (implicit) A's result — all driven by a scripted LLM but
using the REAL tool implementations from Jarvis.

What this guards:
 * `local_list_stories` and `find_story_chapter` keep their JSON contracts.
 * ToolRunner correctly threads tool_results back into delta_messages so the
   next LLM turn sees them.
 * Both tools' real data-path assumptions (DATA_DIR + stories/ layout) hold.

A regression in any of these fails the test with a clear diff.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.harness import ToolCallRecorder, build_scripted_agent, first_tool_result_text


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def seeded_library(tmp_path, monkeypatch):
    """Both library_server and story_server read DATA_DIR — monkeypatch both
    to a temp dir seeded with a single story and chapter file."""
    data_dir = tmp_path / "data"
    story_dir = data_dir / "stories" / "Thần Đạo Đan Tôn"
    story_dir.mkdir(parents=True)
    (story_dir / "001_Thần Đạo Đan Tôn.txt").write_text("chapter 1 text content")

    import tools.library_server as library_server
    import tools.story_server as story_server

    monkeypatch.setattr(library_server, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(story_server, "DATA_DIR", str(data_dir))
    return data_dir


@pytest.mark.asyncio
async def test_list_then_read_chains_two_real_tools(seeded_library):
    from tools.library_server import local_list_stories
    from tools.story_server import find_story_chapter

    recorder = ToolCallRecorder()
    agent = await build_scripted_agent(
        fixture_path=FIXTURES / "audio_reader_list_then_read.yaml",
        tools=[local_list_stories, find_story_chapter],
        agent_name="AudioReaderAgent",
        recorder=recorder,
    )

    final = await agent.generate("tìm và đọc chương 1 Thần Đạo Đan Tôn")

    recorder.assert_matches(
        [
            ("local_list_stories", {}),
            (
                "find_story_chapter",
                {"story_name": "Thần Đạo Đan Tôn", "chapter_number": 1},
            ),
        ]
    )

    # Pull BOTH tool result messages from history to check both contracts.
    tool_result_msgs = [m for m in agent.message_history if m.tool_results]
    assert len(tool_result_msgs) == 2, (
        f"Expected 2 tool_results messages (one per tool call), "
        f"got {len(tool_result_msgs)}"
    )

    list_payload = json.loads(first_tool_result_text(tool_result_msgs[0]))
    assert isinstance(list_payload, list), (
        f"local_list_stories must return a JSON list, got {type(list_payload)}"
    )
    # Nail the element shape so a future refactor to list[dict] (or any other
    # type) breaks this test instead of silently passing via __eq__ semantics.
    assert all(isinstance(e, str) for e in list_payload), (
        f"local_list_stories contract: list[str]; got "
        f"{[type(e).__name__ for e in list_payload]}"
    )
    assert "Thần Đạo Đan Tôn" in list_payload, (
        f"Seeded story missing from list_stories output: {list_payload}"
    )

    read_payload = json.loads(first_tool_result_text(tool_result_msgs[1]))
    assert read_payload["source"] == "local"
    assert "Thần Đạo Đan Tôn" in read_payload["response"]
    assert "chương 1" in read_payload["response"]

    assert "Thần Đạo Đan Tôn" in final.last_text()
