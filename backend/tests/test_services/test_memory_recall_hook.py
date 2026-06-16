"""Recall v2 — always-retrieve, tail-append, change-gate, provenance.

Drives the REAL ``before_llm_call`` hook (`retrieval_hook.create_memory_retrieval_hooks`)
against a fake agent. Retrieval is stubbed to controlled evidence so the HOOK
LOGIC (not the embedding model) is asserted deterministically. The real
semantic-recall behaviour ("what's my job?" → Software Engineer) is covered by
the gated eval suite where BGE is available.
"""
import types

import pytest

from services.memory import retrieval_hook as rh


def _ev(mtype, excerpt):
    return types.SimpleNamespace(memory_type=mtype, excerpt=excerpt, record_id=excerpt[:8])


class _Agent:
    def __init__(self, name="Jarvis"):
        self.name = name
        self.message_history = []

    def load_message_history(self, msgs):
        self.message_history = list(msgs)


class _Runner:
    def __init__(self, agent):
        self._agent = agent


def _settings():
    return types.SimpleNamespace(
        enabled=True, auto_capture_preferences=False, mode="balanced",
        embedding_model="BAAI/bge-m3", embedding_revision="",
        trigger_lexicon_overrides={}, approval_policy="manual",
        pinned_token_budget=1500, evidence_token_budget=2500,
        qdrant_url="http://localhost:59999", vector_backend="qdrant")


def _user(text):
    from fast_agent.mcp.helpers.content_helpers import text_content
    from fast_agent.mcp.prompt_message_extended import PromptMessageExtended
    return PromptMessageExtended(role="user", content=[text_content(text)])


@pytest.fixture()
def wired(monkeypatch):
    import services.memory.settings as ms
    monkeypatch.setattr(ms, "get_memory_settings", lambda: _settings())
    # _settings() has auto_capture_preferences=False, so the fast-lane extractor
    # never fires here → the recall path is exercised in isolation.
    return monkeypatch


async def _run(agent, query, evidence, monkeypatch):
    async def fake_retrieve(owner, q, targets, cfg):
        return evidence
    monkeypatch.setattr(rh, "_retrieve", fake_retrieve)
    hooks = rh.create_memory_retrieval_hooks()
    await hooks.before_llm_call(_Runner(agent), [_user(query)])


def _injected(agent):
    return [m for m in agent.message_history if rh.is_injected_memory(m)]


async def test_always_retrieve_injects_block(wired):
    agent = _Agent()
    await _run(agent, "what is my job?", [_ev("semantic", "User is a Software Engineer")], wired)
    assert len(_injected(agent)) == 1
    blk = agent.message_history[-1]
    assert "Software Engineer" in rh._msg_text(blk)
    assert rh.MEMORY_MARKER in rh._msg_text(blk)


async def test_provenance_channel_and_sentinel(wired):
    agent = _Agent()
    await _run(agent, "q", [_ev("semantic", "a durable fact")], wired)
    blk = agent.message_history[-1]
    assert rh.PROVENANCE_CHANNEL in (blk.channels or {})        # structural flag
    assert rh.is_injected_memory(blk) is True
    assert rh.is_injected_memory(_user("just a normal message")) is False


async def test_change_gate_skips_same_set(wired):
    agent = _Agent()
    ev = [_ev("semantic", "User is a Software Engineer")]
    await _run(agent, "q1", ev, wired)
    await _run(agent, "q2", ev, wired)                          # identical set
    assert len(_injected(agent)) == 1                          # no second block → cache warm


async def test_change_gate_appends_on_change_without_stripping(wired):
    agent = _Agent()
    await _run(agent, "q1", [_ev("semantic", "is a Software Engineer")], wired)
    await _run(agent, "q2", [_ev("semantic", "likes pho")], wired)
    inj = _injected(agent)
    assert len(inj) == 2                                        # append-only
    assert "Software Engineer" in rh._msg_text(inj[0])         # first NOT stripped (prefix stable)
    assert "pho" in rh._msg_text(inj[1])


async def test_no_evidence_leaves_history_untouched(wired):
    agent = _Agent()
    agent.message_history = [_user("prior turn")]
    await _run(agent, "q", [], wired)
    assert len(agent.message_history) == 1
    assert not rh.is_injected_memory(agent.message_history[0])


def test_latest_user_text_ignores_injected_block():
    # An injected memory block must never be mistaken for the user's query.
    blk = rh._build_block_message([_ev("semantic", "User is a Software Engineer")], "k1")
    assert rh._latest_user_text([_user("real question"), blk]) == "real question"


async def test_change_gate_survives_restart_no_duplicate(wired):
    """C1 regression: the gate is derived from HISTORY (the block's recall_key
    channel), not an in-memory attribute. A reloaded conversation that already
    contains the block must NOT get a duplicate when the same set is retrieved
    by a FRESH agent object (the restart case)."""
    ev = [_ev("semantic", "User is a Software Engineer")]
    a1 = _Agent()
    await _run(a1, "q1", ev, wired)
    assert len(_injected(a1)) == 1
    # Simulate restart: brand-new agent, history reloaded from disk (carries the
    # block), no in-memory gate state.
    a2 = _Agent()
    a2.message_history = list(a1.message_history)
    await _run(a2, "q2", ev, wired)              # same set, fresh agent
    assert len(_injected(a2)) == 1               # NO duplicate block


async def test_query_falls_back_to_history_when_delta_has_no_text(wired):
    """H4: a post-tool turn whose delta carries no user text still recalls,
    using the last real user message from history."""
    agent = _Agent()
    agent.message_history = [_user("what is my job?")]
    captured = {}

    async def fake_retrieve(owner, q, targets, cfg):
        captured["q"] = q
        return [_ev("semantic", "Software Engineer")]
    wired.setattr(rh, "_retrieve", fake_retrieve)
    hooks = rh.create_memory_retrieval_hooks()
    await hooks.before_llm_call(_Runner(agent), [])   # empty delta (e.g. tool turn)
    assert captured.get("q") == "what is my job?"
    assert len(_injected(agent)) == 1
