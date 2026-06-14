"""Reproduction: a voice barge-in silently DROPS the prior user message from
the agent's persisted context, so the next turn's agent never sees it
("không thấy" bug — Jarvis re-asks the baby's age right after being told it).

Drives the REAL ``SessionService.resume_and_send`` (the buggy cancellation
rollback at session_service.py:702-718) end-to-end against a real fast-agent
session on disk (isolated tmp dir). Turn 1 is cancelled mid-LLM exactly the way
a barge-in cancels it; the provider absorbs CancelledError and returns an empty
assistant (mirroring the OpenAI/Anthropic providers — see the comment block in
resume_and_send). We then assert the user's words survive into the next turn.

Evidence this matches production (backend_8001.log:1104-1113):
    Tôi đã có gia đình và có một bé nhỏ, 7 tháng tuổi.   ← user
    Generation cancelled by user.                         ← barge-in
    Bé trai.                                              ← next turn, age lost
"""
import asyncio

import pytest

from fast_agent.agents.agent_types import AgentConfig
from fast_agent.agents.tool_agent import ToolAgent
from fast_agent.core.prompt import Prompt
from fast_agent.llm.internal.passthrough import PassthroughLLM
from fast_agent.session.session_manager import SessionManager
from fast_agent.types.llm_stop_reason import LlmStopReason
from services.session_service import SessionService

MSG1 = "Tôi đã có gia đình và có một bé nhỏ, 7 tháng tuổi."
MSG2 = "Bé trai."


class _BargeableLLM(PassthroughLLM):
    """First turn hangs until cancelled, then absorbs the CancelledError and
    returns an empty assistant — exactly what the real providers do on a
    barge-in. Later turns answer immediately."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0
        self.entered = asyncio.Event()

    async def _apply_prompt_provider_specific(self, multipart_messages,
                                              request_params=None, tools=None, is_template=False):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            try:
                await asyncio.Event().wait()        # block until cancelled
            except asyncio.CancelledError:
                return Prompt.assistant("", stop_reason=LlmStopReason.CANCELLED)
        return Prompt.assistant("Dạ vâng ạ.", stop_reason=LlmStopReason.END_TURN)


class _App:
    """Minimal AgentApp surface resume_and_send uses: ._agents + .send()."""

    def __init__(self, agents):
        self._agents = agents

    async def send(self, payload, agent_name=None):
        return str(await self._agents[agent_name].generate(payload))


@pytest.fixture()
def svc(tmp_path):
    agent = ToolAgent(AgentConfig("Jarvis"))
    agent._llm = _BargeableLLM()
    app = _App({"Jarvis": agent})
    s = SessionService()
    s._manager = SessionManager(cwd=tmp_path)   # isolate session storage to tmp
    return s, app, agent


def _history_text(agent) -> str:
    out = []
    for m in getattr(agent, "message_history", []) or []:
        for c in (getattr(m, "content", None) or []):
            out.append(getattr(c, "text", "") or "")
    return "\n".join(out)


@pytest.mark.xfail(
    reason="BUG: session_service.py:702-718 barge-in rollback pops the user "
           "message too (not just the empty assistant), so save_history persists "
           "it away and the next turn never sees it. Remove this marker when fixed.",
    strict=True,
)
async def test_barge_in_preserves_prior_user_message(svc):
    s, app, agent = svc

    # Turn 1: user states the baby is 7 months old; barge-in cancels it mid-LLM.
    task = asyncio.create_task(s.resume_and_send(app, MSG1, None, agent_name="Jarvis"))
    await asyncio.wait_for(agent._llm.entered.wait(), timeout=2.0)
    task.cancel()
    _resp, session_id = await task          # absorbed → returns, rollback runs

    # The persisted history file is what the NEXT turn reloads from. The user
    # really said MSG1 (it shows in the UI) → it must survive here too.
    path = s._manager.get_session(session_id).latest_history_path("Jarvis")
    saved = path.read_text(encoding="utf-8") if path else ""
    assert "7 tháng tuổi" in saved, "barge-in dropped the user's message from the saved context"

    # Turn 2: follow-up. Agent reloads history from disk → must still see MSG1.
    await s.resume_and_send(app, MSG2, session_id, agent_name="Jarvis")
    assert "7 tháng tuổi" in _history_text(agent), "next turn's agent context lost the prior user message"
