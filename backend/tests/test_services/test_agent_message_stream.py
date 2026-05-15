"""Unit tests for services.agent_message_stream.

Covers:
  * trim_message_for_stream — caps large text content + tool_results blocks.
  * emit_message_history_delta — emits exactly one event per new turn,
    keeps cursor on the agent, resets cursor when history is cleared.
  * list_agent_messages / get_agent_turn_full — read-side helpers feeding
    the REST endpoints.

The tests use a tiny stand-in for an agent (a dataclass with
``message_history``) plus a fake activity_stream that captures broadcasts.
We never import fast_agent here — model_dump is exercised against a
PromptMessageExtended built with simple TextContent blocks.
"""

from __future__ import annotations

from typing import Any

import pytest

# Real fast_agent types — we want round-trip dump/parse to match prod shape.
from fast_agent.types import PromptMessageExtended
from mcp.types import TextContent

from services import agent_message_stream as ams


# ── Fixtures ────────────────────────────────────────────────────────────


class _FakeAgent:
    """Minimal stand-in for an in-process agent, exposing message_history."""

    def __init__(self) -> None:
        self.message_history: list[PromptMessageExtended] = []


@pytest.fixture(autouse=True)
def _patch_activity_stream(monkeypatch: pytest.MonkeyPatch):
    """Replace activity_stream_manager.broadcast with a list capture.

    Also clears the per-agent recent-turns cache between tests so each
    case sees a clean slate (the cache is module-global by design).
    """
    captured: list[dict] = []

    class _FakeStream:
        @staticmethod
        def broadcast(event: dict) -> None:
            captured.append(event)

    import services.activity_stream as act

    monkeypatch.setattr(act, "activity_stream_manager", _FakeStream())
    ams.reset_recent_turns()
    yield captured
    ams.reset_recent_turns()


def _user_msg(text: str) -> PromptMessageExtended:
    return PromptMessageExtended(role="user", content=[TextContent(type="text", text=text)])


def _asst_msg(text: str) -> PromptMessageExtended:
    return PromptMessageExtended(role="assistant", content=[TextContent(type="text", text=text)])


# ── trim_message_for_stream ─────────────────────────────────────────────


def test_trim_short_content_unchanged():
    payload = {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    out = ams.trim_message_for_stream(payload)
    block = out["content"][0]
    assert block["text"] == "hello"
    assert "_truncated" not in block
    assert "_full_size" not in block


def test_trim_long_content_marks_truncated():
    big = "x" * (ams.MAX_BLOCK_TEXT_BYTES + 1024)
    payload = {"role": "assistant", "content": [{"type": "text", "text": big}]}
    out = ams.trim_message_for_stream(payload)
    block = out["content"][0]
    assert len(block["text"].encode("utf-8")) <= ams.MAX_BLOCK_TEXT_BYTES
    assert block["_truncated"] is True
    assert block["_full_size"] == len(big.encode("utf-8"))


def test_trim_tool_result_content_blocks():
    big = "y" * (ams.MAX_BLOCK_TEXT_BYTES + 5)
    payload = {
        "role": "user",
        "tool_results": {
            "tid_1": {"content": [{"type": "text", "text": big}]},
            "tid_2": {"content": [{"type": "text", "text": "small"}]},
        },
    }
    out = ams.trim_message_for_stream(payload)
    a = out["tool_results"]["tid_1"]["content"][0]
    b = out["tool_results"]["tid_2"]["content"][0]
    assert a["_truncated"] is True
    assert a["_full_size"] == len(big.encode("utf-8"))
    assert b["text"] == "small"
    assert "_truncated" not in b


def test_trim_handles_missing_keys_gracefully():
    # No content key, no tool_results — should not raise.
    payload = {"role": "assistant"}
    out = ams.trim_message_for_stream(payload)
    assert out is payload


def test_trim_non_text_block_left_alone():
    payload = {
        "role": "user",
        "content": [
            {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}
        ],
    }
    out = ams.trim_message_for_stream(payload)
    assert out["content"][0]["type"] == "image"
    assert "_truncated" not in out["content"][0]


# ── emit_message_history_delta ─────────────────────────────────────────


def test_emit_first_run_emits_all_turns(_patch_activity_stream):
    captured = _patch_activity_stream
    agent = _FakeAgent()
    agent.message_history = [_user_msg("hi"), _asst_msg("hello")]

    n = ams.emit_message_history_delta(agent, "TestAgent", "run-1")

    assert n == 2
    assert len(captured) == 2
    assert captured[0]["agent_name"] == "TestAgent"
    assert captured[0]["event_type"] == "message_turn"
    assert captured[0]["data"]["turn_idx"] == 0
    assert captured[0]["data"]["role"] == "user"
    assert captured[1]["data"]["turn_idx"] == 1
    assert captured[1]["data"]["role"] == "assistant"
    assert getattr(agent, ams.HISTORY_CURSOR_ATTR) == 2


def test_emit_only_delta_after_initial(_patch_activity_stream):
    captured = _patch_activity_stream
    agent = _FakeAgent()
    agent.message_history = [_user_msg("a"), _asst_msg("b")]
    ams.emit_message_history_delta(agent, "T", "r1")
    captured.clear()

    # Append two more turns
    agent.message_history.append(_user_msg("c"))
    agent.message_history.append(_asst_msg("d"))
    n = ams.emit_message_history_delta(agent, "T", "r1")

    assert n == 2
    assert [e["data"]["turn_idx"] for e in captured] == [2, 3]
    assert getattr(agent, ams.HISTORY_CURSOR_ATTR) == 4


def test_emit_no_op_when_nothing_new(_patch_activity_stream):
    captured = _patch_activity_stream
    agent = _FakeAgent()
    agent.message_history = [_user_msg("hi")]
    ams.emit_message_history_delta(agent, "T", "r1")
    captured.clear()

    # Same history — second call should emit nothing.
    n = ams.emit_message_history_delta(agent, "T", "r1")
    assert n == 0
    assert captured == []


def test_emit_resets_cursor_when_history_cleared(_patch_activity_stream):
    captured = _patch_activity_stream
    agent = _FakeAgent()
    agent.message_history = [_user_msg("a"), _asst_msg("b"), _user_msg("c")]
    ams.emit_message_history_delta(agent, "T", "r1")
    captured.clear()

    # Simulate /clear or new conversation: history shrinks.
    agent.message_history = [_user_msg("fresh")]
    n = ams.emit_message_history_delta(agent, "T", "r2")

    assert n == 1
    assert captured[0]["data"]["turn_idx"] == 0
    assert captured[0]["data"]["role"] == "user"
    assert captured[0]["run_id"] == "r2"
    assert getattr(agent, ams.HISTORY_CURSOR_ATTR) == 1


def test_emit_truncates_large_blocks_in_payload(_patch_activity_stream):
    captured = _patch_activity_stream
    big = "z" * (ams.MAX_BLOCK_TEXT_BYTES + 100)
    agent = _FakeAgent()
    agent.message_history = [
        PromptMessageExtended(role="assistant", content=[TextContent(type="text", text=big)])
    ]

    ams.emit_message_history_delta(agent, "T", "r1")

    assert len(captured) == 1
    block = captured[0]["data"]["message"]["content"][0]
    assert block["_truncated"] is True
    assert block["_full_size"] == len(big.encode("utf-8"))


def test_emit_swallows_history_access_errors(_patch_activity_stream):
    captured = _patch_activity_stream

    class _Broken:
        @property
        def message_history(self):
            raise RuntimeError("bork")

    n = ams.emit_message_history_delta(_Broken(), "T", "r1")
    assert n == 0
    assert captured == []


# ── list_agent_messages ─────────────────────────────────────────────────


@pytest.fixture
def _live_agent_app(monkeypatch: pytest.MonkeyPatch):
    """Plug a fake agent_app into services.shared_state so the resolver picks it up."""
    import services.shared_state as state

    agents = {}

    class _FakeAgentApp:
        def __init__(self) -> None:
            self._agents = agents

    monkeypatch.setattr(state, "agent_app", _FakeAgentApp(), raising=False)
    return agents


def test_list_messages_uses_live_history(_live_agent_app):
    agent = _FakeAgent()
    agent.message_history = [_user_msg("u1"), _asst_msg("a1"), _user_msg("u2")]
    _live_agent_app["LiveOne"] = agent

    out = ams.list_agent_messages("LiveOne")
    assert out["total"] == 3
    assert [t["turn_idx"] for t in out["turns"]] == [0, 1, 2]
    assert [t["role"] for t in out["turns"]] == ["user", "assistant", "user"]


def test_list_messages_since_returns_delta(_live_agent_app):
    agent = _FakeAgent()
    agent.message_history = [_user_msg("u1"), _asst_msg("a1"), _user_msg("u2"), _asst_msg("a2")]
    _live_agent_app["DeltaOne"] = agent

    out = ams.list_agent_messages("DeltaOne", since=2)
    assert out["total"] == 4
    assert [t["turn_idx"] for t in out["turns"]] == [2, 3]


def test_list_messages_limit_returns_latest_window(_live_agent_app):
    agent = _FakeAgent()
    agent.message_history = [_user_msg(f"m{i}") for i in range(20)]
    _live_agent_app["WindowOne"] = agent

    out = ams.list_agent_messages("WindowOne", since=0, limit=5)
    assert out["total"] == 20
    assert [t["turn_idx"] for t in out["turns"]] == [15, 16, 17, 18, 19]
    assert out["start"] == 15


def test_list_messages_unknown_agent_returns_empty(_live_agent_app):
    out = ams.list_agent_messages("Ghost")
    assert out == {"turns": [], "total": 0}


# ── get_agent_turn_full ────────────────────────────────────────────────


def test_get_full_returns_untruncated_block(_live_agent_app):
    big = "Q" * (ams.MAX_BLOCK_TEXT_BYTES + 200)
    agent = _FakeAgent()
    agent.message_history = [_asst_msg(big)]
    _live_agent_app["FullOne"] = agent

    out = ams.get_agent_turn_full("FullOne", 0)
    assert out is not None
    assert out["turn_idx"] == 0
    block = out["message"]["content"][0]
    assert len(block["text"].encode("utf-8")) == len(big.encode("utf-8"))
    assert "_truncated" not in block


def test_get_full_returns_none_for_out_of_range(_live_agent_app):
    agent = _FakeAgent()
    agent.message_history = [_user_msg("only")]
    _live_agent_app["TinyOne"] = agent

    assert ams.get_agent_turn_full("TinyOne", 5) is None
    assert ams.get_agent_turn_full("TinyOne", -1) is None
    assert ams.get_agent_turn_full("Missing", 0) is None


# ── Recent-turns cache (covers ephemeral clone agents) ─────────────────


def test_emit_records_full_payload_in_cache(_patch_activity_stream):
    """Each emitted turn lands in the per-agent cache — used by the read
    endpoints after the live (clone) agent has been discarded."""
    big = "Q" * (ams.MAX_BLOCK_TEXT_BYTES + 100)
    agent = _FakeAgent()
    agent.message_history = [
        _user_msg("ask"),
        PromptMessageExtended(role="assistant", content=[TextContent(type="text", text=big)]),
    ]
    ams.emit_message_history_delta(agent, "FinanceClone", "run-1")

    cached = ams.get_recent_turns("FinanceClone")
    assert [t["turn_idx"] for t in cached] == [0, 1]
    # The cached blob is the FULL untruncated text — we expose it via
    # ``/turns/{idx}/full`` so users can read the whole tool_result on demand.
    assistant_text = cached[1]["message"]["content"][0]["text"]
    assert len(assistant_text.encode("utf-8")) == len(big.encode("utf-8"))
    assert "_truncated" not in cached[1]["message"]["content"][0]


def test_list_messages_falls_back_to_cache_when_live_history_empty(_patch_activity_stream, _live_agent_app):
    """Reproducer for the FinanceAgent bug: live agent has empty history
    (because each tool call ran on a discarded clone) but emitted turns
    are in the cache. ``list_agent_messages`` must serve from there."""
    # Persistent template lives in the registry but its history is empty
    # — this mirrors how ``agent__FinanceAgent`` runs against a clone.
    template = _FakeAgent()
    _live_agent_app["FinanceClone"] = template

    # A clone ran and emitted history; the cache captured everything.
    clone = _FakeAgent()
    clone.message_history = [_user_msg("hi"), _asst_msg("response")]
    ams.emit_message_history_delta(clone, "FinanceClone", "run-1")

    # The persistent template is still empty; the read endpoint must serve
    # the cached turns instead of returning a misleading empty list.
    assert template.message_history == []
    out = ams.list_agent_messages("FinanceClone")
    assert out["total"] == 2
    assert [t["turn_idx"] for t in out["turns"]] == [0, 1]
    assert out["turns"][0]["role"] == "user"
    assert out["turns"][1]["role"] == "assistant"


def test_get_full_serves_cache_when_live_history_empty(_patch_activity_stream, _live_agent_app):
    template = _FakeAgent()
    _live_agent_app["FinanceClone"] = template

    big = "B" * (ams.MAX_BLOCK_TEXT_BYTES + 50)
    clone = _FakeAgent()
    clone.message_history = [
        PromptMessageExtended(role="assistant", content=[TextContent(type="text", text=big)])
    ]
    ams.emit_message_history_delta(clone, "FinanceClone", "run-1")

    out = ams.get_agent_turn_full("FinanceClone", 0)
    assert out is not None
    assert out["turn_idx"] == 0
    block = out["message"]["content"][0]
    # The full endpoint serves UNTRUNCATED content (cache stores the full payload).
    assert len(block["text"].encode("utf-8")) == len(big.encode("utf-8"))
    assert "_truncated" not in block


def test_list_messages_prefers_live_history_over_cache(_patch_activity_stream, _live_agent_app):
    """Persistent agents (e.g. Jarvis) own their history; cache is only
    consulted as a fallback. We must prefer live data so we don't ever
    serve stale cached entries when the live source is authoritative."""
    live = _FakeAgent()
    live.message_history = [_user_msg("live-1"), _asst_msg("live-2")]
    _live_agent_app["Persistent"] = live

    # Pollute cache with stale turns under the same name
    ams._record_recent_turn("Persistent", 0, {"role": "user", "content": [{"type": "text", "text": "stale"}]})

    out = ams.list_agent_messages("Persistent")
    assert out["total"] == 2
    assert out["turns"][0]["message"]["content"][0]["text"] == "live-1"


def test_emit_truncates_broadcast_but_keeps_cache_full(_patch_activity_stream):
    """The broadcast version is trimmed (small SSE chunks); the cached
    version stays full so /turns/{idx}/full can recover the original."""
    captured = _patch_activity_stream
    big = "T" * (ams.MAX_BLOCK_TEXT_BYTES + 1000)
    agent = _FakeAgent()
    agent.message_history = [
        PromptMessageExtended(role="assistant", content=[TextContent(type="text", text=big)])
    ]
    ams.emit_message_history_delta(agent, "X", "r1")

    broadcast_block = captured[0]["data"]["message"]["content"][0]
    assert broadcast_block["_truncated"] is True
    assert len(broadcast_block["text"].encode("utf-8")) <= ams.MAX_BLOCK_TEXT_BYTES

    cached_block = ams.get_recent_turns("X")[0]["message"]["content"][0]
    assert "_truncated" not in cached_block
    assert len(cached_block["text"].encode("utf-8")) == len(big.encode("utf-8"))


def test_recent_turn_cache_caps_size():
    """Cache must not grow unbounded — emitting more turns than the cap
    drops the oldest entries while keeping turn_idx sortable."""
    name = "BigOne"
    cap = ams._RECENT_TURNS_PER_AGENT_CAP
    for i in range(cap + 25):
        ams._record_recent_turn(name, i, {"role": "user", "content": [{"type": "text", "text": f"m{i}"}]})

    cached = ams.get_recent_turns(name)
    assert len(cached) == cap
    # Oldest 25 dropped, latest cap kept.
    assert cached[0]["turn_idx"] == 25
    assert cached[-1]["turn_idx"] == cap + 24


def test_recent_turn_cache_replaces_existing_idx():
    """Re-recording the same turn_idx (e.g. a clone re-ran from idx 0)
    overwrites in place rather than appending duplicates."""
    name = "Replacing"
    ams._record_recent_turn(name, 0, {"role": "user", "content": [{"text": "v1"}]})
    ams._record_recent_turn(name, 1, {"role": "assistant", "content": [{"text": "old"}]})
    ams._record_recent_turn(name, 1, {"role": "assistant", "content": [{"text": "new"}]})

    cached = ams.get_recent_turns(name)
    assert len(cached) == 2
    assert cached[1]["message"]["content"][0]["text"] == "new"
