"""WS04/WS08 routing brain: English-only lexicon (§7) + multilingual embedding
intent gate, level decisions, escalation, quality gate."""

import pytest

from helpers import memory_triggers as mt
from services.retrieval import intent_router as ir
from services.retrieval import quality_gate as qg
from services.retrieval.budget import build_budget
from services.retrieval.contracts import Evidence, EvidenceScores, EvidenceSource


# ── English lexicon fast-path / degraded fallback (English-only by §7) ──

@pytest.mark.parametrize("q,target", [
    ("what did we do last time", mt.TARGET_EPISODIC),
    ("from now on always answer concisely", mt.TARGET_PINNED),
    ("how do we usually deploy", mt.TARGET_PROCEDURAL),
    ("email from the PM", mt.TARGET_COMMUNICATIONS),
])
def test_classify_targets_english(q, target):
    assert target in mt.classify_targets(q)


def test_lexicon_is_english_only():
    # Non-English does NOT hit the substring lexicon — the embedding gate is
    # what makes detection multilingual (see test_intent_gate_* below).
    assert mt.classify_targets("lần trước mình làm thế nào") == set()


def test_identifier_detection():
    assert mt.has_exact_identifier("the bug is in services/foo.py")
    assert mt.has_exact_identifier("ticket PROJ-123 again")
    assert mt.has_exact_identifier("hit `validateUser` here")
    assert not mt.has_exact_identifier("how are you today")


# ── router ──

def test_level0_for_social_no_signal():
    d = ir.decide_initial("hello, how are you?")
    assert d.level == ir.LEVEL_NONE


def test_level0_external_only():
    # English lexicon: "latest"/"today's" → external only → stays Level 0.
    d = ir.decide_initial("what's the latest gold price today")
    assert d.level == ir.LEVEL_NONE


def test_level1_on_gate_targets_multilingual():
    # The embedding gate supplies targets for ANY language; here a Vietnamese
    # query is detected by the gate and passed in as gate_targets.
    d = ir.decide_initial("lần trước chúng ta deploy ra sao",
                          gate_targets={mt.TARGET_EPISODIC})
    assert d.level == ir.LEVEL_FAST
    assert mt.TARGET_EPISODIC in d.targets and d.reason == "intent gate"


def test_level1_on_identifier_bm25_first():
    d = ir.decide_initial("what was the fix in backend/server.py")
    assert d.level == ir.LEVEL_FAST and d.bm25_first is True


def test_level0_when_ledger_sufficient_or_tool_loop():
    assert ir.decide_initial("last time", ledger_has_sufficient=True).level == ir.LEVEL_NONE
    assert ir.decide_initial("last time", continuing_tool_loop=True).level == ir.LEVEL_NONE


def test_agent_requested_forces_fast():
    d = ir.decide_initial("anything", agent_requested=True)
    assert d.level == ir.LEVEL_FAST


def test_escalation_rules():
    # balanced: 1 round allowed, weak → escalate
    assert ir.should_escalate(weak=True, mode="balanced", deep_requested=False,
                              high_risk=False, rounds_used=0, max_rounds=1) is True
    # rounds exhausted → no
    assert ir.should_escalate(weak=True, mode="balanced", deep_requested=False,
                              high_risk=False, rounds_used=1, max_rounds=1) is False
    # economical: 0 rounds → never
    assert ir.should_escalate(weak=True, mode="economical", deep_requested=True,
                              high_risk=True, rounds_used=0, max_rounds=0) is False


# ── quality gate ──

def _ev(final, bm25=1, dense=1):
    return Evidence("e", "r", "Jarvis", "semantic", "x",
                    EvidenceSource("session_message", "d"),
                    EvidenceScores(bm25_rank=bm25, dense_rank=dense, final=final),
                    "user_confirmed", 0.9)


def test_quality_gate():
    assert qg.is_weak([]) is True                              # no results
    assert qg.is_weak([_ev(0.001)]) is True                    # low score
    assert qg.is_weak([_ev(0.05, bm25=1, dense=20)]) is True   # rank disagreement
    assert qg.is_weak([_ev(0.05, bm25=1, dense=2)]) is False   # healthy


# ── budget ──

def test_budget_presets_and_override():
    assert build_budget("economical").max_evidence_tokens == 1000
    assert build_budget("balanced").max_evidence_tokens == 2500
    assert build_budget("deep").max_evidence_tokens == 5000
    assert build_budget("balanced", evidence_token_budget=1234).max_evidence_tokens == 1234
    # unknown mode falls back to balanced
    assert build_budget("nope").max_evidence_tokens == 2500
