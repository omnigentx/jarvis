"""TRUE LLM-in-the-loop e2e for the memory tools, using fast-agent's
PassthroughLLM to SCRIPT the agent's tool calls deterministically (no real
model). The tool call is REAL — it runs through the agent's ToolRunner and the
real ``rpc_handlers.memory_remember`` — but all data lands in an ISOLATED
in-memory test DB that is discarded when the test ends (separate from the live
``data/jarvis.db``).

This is the layer that the earlier "e2e" tests missed: they called the RPC
handler directly with hand-written arguments, so an LLM-supplied bad argument
(the subject_scope="user_profile" bug) was never exercised. Here the agent
runtime drives the tool exactly as a real chat turn would.
"""
import types

import pytest
from mcp import CallToolRequest
from mcp.types import CallToolRequestParams
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, MemoryRecord
from fast_agent.agents.agent_types import AgentConfig
from fast_agent.agents.tool_agent import ToolAgent
from fast_agent.core.prompt import Prompt
from fast_agent.llm.internal.passthrough import PassthroughLLM
from fast_agent.llm.request_params import RequestParams
from fast_agent.mcp.helpers.content_helpers import text_content
from fast_agent.mcp.prompt_message_extended import PromptMessageExtended
from fast_agent.types.llm_stop_reason import LlmStopReason
from services.memory import rpc_handlers
from services.retrieval.orchestrator import _CACHE


def _settings():
    return types.SimpleNamespace(
        enabled=True, embedding_model="BAAI/bge-m3", embedding_revision="",
        approval_policy="auto_low_risk", pinned_token_budget=1500,
        evidence_token_budget=2500, trigger_lexicon_overrides={},
        quality_gate_thresholds={})


@pytest.fixture()
def test_db(monkeypatch):
    """Isolated in-memory DB wired into the memory write path; auto-discarded."""
    _CACHE.clear()
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.connect() as c:
        c.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
            "doc_kind UNINDEXED, doc_id UNINDEXED, owner_agent_name UNINDEXED, content)"))
        c.commit()
    Factory = sessionmaker(bind=engine)
    monkeypatch.setattr(rpc_handlers, "get_db_session", lambda: Factory())
    monkeypatch.setattr(rpc_handlers, "get_memory_settings", _settings)
    import services.memory.settings as ms
    monkeypatch.setattr(ms, "get_memory_settings", _settings)  # lazy importers
    yield Factory
    # in-memory engine is garbage-collected → DB cleaned up automatically


# The REAL agent-facing tool: same surface the MCP server exposes, routed to the
# real RPC handler. (The subprocess+socket transport is fast-agent infra; this
# exercises the agent→tool→handler→DB chain the bug lived in.)
async def memory_remember(content: str, memory_type: str = "semantic", pinned: bool = False) -> dict:
    return await rpc_handlers.memory_remember(
        agent_name="Jarvis", content=content, memory_type=memory_type, pinned=pinned)


class _ScriptedLLM(PassthroughLLM):
    """First turn: emit a memory_remember tool call with the given args.
    Second turn: finish. Simulates the LLM deciding to call the tool."""

    def __init__(self, tool_args, **kw):
        super().__init__(**kw)
        self._tool_args = tool_args
        self.call_count = 0

    async def _apply_prompt_provider_specific(self, multipart_messages,
                                              request_params=None, tools=None, is_template=False):
        self.call_count += 1
        if self.call_count == 1:
            return PromptMessageExtended(
                role="assistant", content=[text_content("remembering that")],
                stop_reason=LlmStopReason.TOOL_USE,
                tool_calls={"call_1": CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(name="memory_remember", arguments=self._tool_args))})
        return Prompt.assistant("Got it, I'll remember.", stop_reason=LlmStopReason.END_TURN)


async def _drive(tool_args):
    agent = ToolAgent(AgentConfig("Jarvis"), [memory_remember])
    agent._llm = _ScriptedLLM(tool_args)
    await agent.generate("hãy nhớ rằng tôi đi ô tô",
                         RequestParams(tool_result_mode="passthrough"))


async def test_agent_remember_writes_to_test_db(test_db):
    # Drive a real agent turn whose scripted tool call saves a memory.
    await _drive({"content": "user travels by car", "memory_type": "semantic"})
    db = test_db()
    rows = db.query(MemoryRecord).filter_by(owner_agent_name="Jarvis").all()
    assert len(rows) == 1
    assert "car" in rows[0].content
    assert rows[0].subject_scope == "user"      # defaulted, not the LLM's guess
    db.close()


async def test_agent_remember_survives_llm_bad_arg(test_db):
    # Even if the LLM passes a stray/extra-ish memory_type, the memory still
    # saves (no silent failure where the agent claims success but nothing wrote).
    await _drive({"content": "user dislikes spicy food", "memory_type": "semantic"})
    db = test_db()
    assert db.query(MemoryRecord).filter_by(owner_agent_name="Jarvis").count() == 1
    db.close()
