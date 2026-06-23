"""WS08b auto-inject hook: gating, injection, self-replacing block."""

import types

import pytest

from services.memory import retrieval_hook as rh
from services.retrieval.contracts import Evidence, EvidenceScores, EvidenceSource


def _settings(enabled=True, auto_capture=False):
    return types.SimpleNamespace(
        enabled=enabled, embedding_model="BAAI/bge-m3", embedding_revision="",
        trigger_lexicon_overrides={}, mode="balanced",
        evidence_token_budget=2500,
        quality_gate_thresholds={}, auto_capture_preferences=auto_capture,
        approval_policy="manual", pinned_token_budget=1500)


class _Block:
    def __init__(self, text): self.text = text


class _Msg:
    def __init__(self, role, text): self.role = role; self.content = [_Block(text)]


class _Agent:
    def __init__(self, name, hist=None):
        self.name = name
        self._message_history = list(hist or [])
    @property
    def message_history(self): return self._message_history
    def load_message_history(self, msgs): self._message_history = list(msgs or [])


class _Runner:
    def __init__(self, agent): self._agent = agent


def _ev(rid="m1"):
    return Evidence(f"memory:{rid}", rid, "Jarvis", "semantic", "use a dedicated compactor",
                   EvidenceSource("memory_record", rid), EvidenceScores(final=0.5),
                   "user_confirmed", 0.9)


async def _run_hook(monkeypatch, *, query, settings, hist=None, evidence=None):
    monkeypatch.setattr(rh, "get_memory_settings", lambda: settings, raising=False)
    # patch the module function imported lazily inside the hook
    import services.memory.settings as ms
    monkeypatch.setattr(ms, "get_memory_settings", lambda: settings)
    async def fake_retrieve(owner, q, targets, cfg): return evidence or []
    monkeypatch.setattr(rh, "_retrieve", fake_retrieve)
    agent = _Agent("Jarvis", hist)
    hooks = rh.create_memory_retrieval_hooks()
    delta = [_Msg("user", query)]
    await hooks.before_llm_call(_Runner(agent), delta)
    # Mirror the framework: the delta (incl. the recall block the hook appended
    # AFTER the user message) is committed to message_history when the turn ends.
    agent.load_message_history(list(agent.message_history) + delta)
    return agent


async def test_injects_block_on_identifier_signal(monkeypatch):
    agent = await _run_hook(monkeypatch, query="what was the fix in backend/server.py",
                            settings=_settings(), evidence=[_ev()])
    texts = [rh._msg_text(m) for m in agent.message_history]
    assert any(rh.MEMORY_MARKER in t for t in texts)
    assert any("dedicated compactor" in t for t in texts)
    # #1 ordering: the block lands AFTER the user message, never before it.
    assert not rh.is_injected_memory(agent.message_history[0])
    assert rh.is_injected_memory(agent.message_history[-1])


async def test_no_injection_when_disabled(monkeypatch):
    agent = await _run_hook(monkeypatch, query="what was the fix in backend/server.py",
                            settings=_settings(enabled=False), evidence=[_ev()])
    assert not any(rh.is_injected_memory(m) for m in agent.message_history)


async def test_emits_live_recall_sse_matching_the_block(monkeypatch):
    # The "memories used" chip must render DURING the turn (not only after a
    # reload): on a fresh injection the hook broadcasts a `memory_recalled`
    # activity event carrying the SAME content + per-line lanes + scores the
    # block persists, so the live chip equals the reloaded one.
    events = []
    import services.activity_stream as asm
    monkeypatch.setattr(asm.activity_stream_manager, "broadcast", lambda ev: events.append(ev))
    await _run_hook(monkeypatch, query="what was the fix in backend/server.py",
                    settings=_settings(), evidence=[_ev()])
    recalls = [e for e in events if e.get("event_type") == "memory_recalled"]
    assert len(recalls) == 1
    data = recalls[0]["data"]
    assert recalls[0]["agent_name"] == "Jarvis"
    assert rh.MEMORY_MARKER in data["content"]
    assert "dedicated compactor" in data["content"]
    # one lane-list and one score per injected line, SAME order as the block.
    assert len(data["recall_lanes"]) == 1
    assert len(data["recall_scores"]) == 1
    assert set(data["recall_scores"][0]) == {"rrf", "rerank", "conf", "authority"}


async def test_no_recall_sse_when_no_evidence(monkeypatch):
    events = []
    import services.activity_stream as asm
    monkeypatch.setattr(asm.activity_stream_manager, "broadcast", lambda ev: events.append(ev))
    await _run_hook(monkeypatch, query="hello there", settings=_settings(), evidence=[])
    assert not any(e.get("event_type") == "memory_recalled" for e in events)


# NOTE: v1 "Level 0 gating", "self-replacing block" and "strips stale block"
# tests were removed — recall v2 is ALWAYS-retrieve + append-only + change-gate
# (no intent gate deciding recall, no mid-history strip). The v2
# injection / provenance / change-gate / no-evidence cases live in
# test_memory_recall_hook.py.


async def _drive_n(monkeypatch, *, auto_capture, n):
    """Drive the hook ``n`` times on ONE agent; capture extractor firings."""
    import asyncio
    import services.memory.settings as ms
    monkeypatch.setattr(ms, "get_memory_settings", lambda: _settings(auto_capture=auto_capture))

    async def no_retrieve(owner, q, targets, cfg):
        return []
    monkeypatch.setattr(rh, "_retrieve", no_retrieve)
    fired = []

    async def fake_extract(owner, snippet, cfg):
        fired.append((owner, snippet))
    monkeypatch.setattr(rh, "_run_extraction", fake_extract)

    agent = _Agent("Jarvis")
    hooks = rh.create_memory_retrieval_hooks()
    for i in range(n):
        await hooks.before_llm_call(_Runner(agent), [_Msg("user", f"turn {i}: I like pho")])
    await asyncio.sleep(0)
    return fired


async def test_extraction_fires_once_per_debounce_window(monkeypatch):
    # Frequency gate: the cheap extractor fires once every EXTRACT_EVERY_N turns,
    # not every turn (cost control) and not via content classification.
    fired = await _drive_n(monkeypatch, auto_capture=True, n=rh.EXTRACT_EVERY_N)
    assert len(fired) == 1 and fired[0][0] == "Jarvis"
    assert "pho" in fired[0][1]                     # snippet carries the recent turns


async def test_extraction_off_when_disabled(monkeypatch):
    fired = await _drive_n(monkeypatch, auto_capture=False, n=rh.EXTRACT_EVERY_N + 2)
    assert fired == []                              # setting off → never fires
