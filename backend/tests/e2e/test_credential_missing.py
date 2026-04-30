"""E2E regression: tools that need credentials must fail loud when creds are
missing, never silently return empty/success data.

User's single-user fail-loud policy (memory: feedback_no_silent_fallbacks):
"binary works/errors, delete legacy-compat branches".

What this guards:
 * Gmail tool (`gmail_list_labels`) returns an error string that clearly names
   the missing credential + how to fix it — not an empty list or an ambiguous
   success message.
 * The LLM, seeing this error in tool_results, still gets to respond to the
   user — we don't assert that, only that the error bubbled up cleanly.

If someone "helpfully" swallows the RuntimeError or returns [] silently when
creds are missing, this test catches it.
"""

from __future__ import annotations

import pytest

from fast_agent.agents.agent_types import AgentConfig
from fast_agent.agents.tool_agent import ToolAgent

from tests.e2e.harness import ToolCallRecorder, first_tool_result_text
from tests.e2e.scripted_llm import ScriptedLLM


@pytest.mark.asyncio
async def test_gmail_list_labels_fails_loud_without_creds(monkeypatch):
    import services.google_oauth as google_oauth
    monkeypatch.setattr(google_oauth, "get_credentials", lambda **_: None)

    from tools.gmail_server import gmail_list_labels

    recorder = ToolCallRecorder()

    llm = ScriptedLLM(
        turns=[
            {
                "tool_calls": {
                    "c1": {"name": "gmail_list_labels", "arguments": {}}
                }
            },
            {"content": "reported error to user"},
        ]
    )
    agent = ToolAgent(
        config=AgentConfig(
            name="mail_agent",
            instruction="test",
            servers=[],
            human_input=False,
        ),
        tools=[gmail_list_labels],
        context=None,
    )
    agent._llm = llm
    from fast_agent.agents.tool_runner import ToolRunnerHooks
    agent.tool_runner_hooks = ToolRunnerHooks(before_tool_call=recorder.hook)

    await agent.generate("liệt kê các label gmail")

    recorder.assert_matches([("gmail_list_labels", {})])

    tool_result_msgs = [m for m in agent.message_history if m.tool_results]
    assert tool_result_msgs, "Expected a tool_results message"
    raw = first_tool_result_text(tool_result_msgs[0])

    # Must surface the real failure reason — not silently return []. The
    # exact string is the contract; a permissive substring match on
    # "connected" would accept "successfully connected" and silently pass.
    assert "Gmail is not connected" in raw, (
        f"gmail_list_labels silently swallowed missing creds; got: {raw!r}"
    )
