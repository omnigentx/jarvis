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
        pinned_token_budget=1500, evidence_token_budget=2500)


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
    delta = [_user(query)]
    await hooks.before_llm_call(_Runner(agent), delta)
    # Mirror the framework: the delta (incl. any recall block the hook appended
    # AFTER the user message) is committed to message_history when the turn ends.
    agent.message_history.extend(delta)
    return delta


def _injected(agent):
    return [m for m in agent.message_history if rh.is_injected_memory(m)]


async def test_always_retrieve_injects_block(wired):
    agent = _Agent()
    await _run(agent, "what is my job?", [_ev("semantic", "User is a Software Engineer")], wired)
    assert len(_injected(agent)) == 1
    blk = agent.message_history[-1]
    assert "Software Engineer" in rh._msg_text(blk)
    assert rh.MEMORY_MARKER in rh._msg_text(blk)


async def test_block_lands_after_user_message(wired):
    """#1 fix: the recall block must come AFTER the user's message, not before it
    (it appended to message_history ahead of the user turn previously)."""
    agent = _Agent()
    await _run(agent, "plan my trip", [_ev("semantic", "drives a car")], wired)
    h = agent.message_history
    assert len(h) == 2
    assert not rh.is_injected_memory(h[0])          # user message FIRST
    assert rh._msg_text(h[0]) == "plan my trip"
    assert rh.is_injected_memory(h[1])              # recall block AFTER


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


async def test_no_evidence_injects_no_block(wired):
    agent = _Agent()
    agent.message_history = [_user("prior turn")]
    await _run(agent, "q", [], wired)
    assert len(_injected(agent)) == 0       # no evidence → no recall block at all


def test_latest_user_text_ignores_injected_block():
    # An injected memory block must never be mistaken for the user's query.
    blk = rh._build_block_message([_ev("semantic", "User is a Software Engineer")])
    assert rh._latest_user_text([_user("real question"), blk]) == "real question"


async def test_partial_overlap_injects_only_new_ids(wired):
    """Per-record_id dedup: a turn whose relevant set OVERLAPS an earlier recall
    must inject ONLY the genuinely-new memories — never re-emit the ones already
    in context (the duplication that wasted tokens with the old whole-set gate),
    and never touch the earlier block (KV prefix cache stays warm)."""
    agent = _Agent()
    a = _ev("semantic", "AAA likes travel")
    b = _ev("semantic", "BBB drives a car")
    c = _ev("semantic", "CCC works at a bank")
    await _run(agent, "q1", [a, b], wired)            # recall {A, B}
    await _run(agent, "q2", [b, c], wired)            # recall {B, C} — B overlaps
    inj = _injected(agent)
    assert len(inj) == 2                              # second block appended, first intact
    assert "drives a car" in rh._msg_text(inj[0])     # B was in the FIRST block
    second = rh._msg_text(inj[1])
    assert "works at a bank" in second                # only the NEW one (C)
    assert "drives a car" not in second               # B NOT repeated
    # The second block's id channel carries only C.
    assert rh._block_recall_ids(inj[1]) == [c.record_id]


async def test_all_overlap_appends_nothing(wired):
    """If every relevant memory is already in context, no block is appended at
    all → the prefix cache is fully preserved."""
    agent = _Agent()
    ev = [_ev("semantic", "AAA likes travel"), _ev("semantic", "BBB drives a car")]
    await _run(agent, "q1", ev, wired)
    await _run(agent, "q2", list(reversed(ev)), wired)   # same ids, different order
    assert len(_injected(agent)) == 1


async def test_change_gate_survives_restart_no_duplicate(wired):
    """C1 regression: the gate is derived from HISTORY (the block's recall_ids
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
    delta = []                                        # empty delta (e.g. tool turn)
    await hooks.before_llm_call(_Runner(agent), delta)
    agent.message_history.extend(delta)               # framework merges delta after
    assert captured.get("q") == "what is my job?"
    assert len(_injected(agent)) == 1
