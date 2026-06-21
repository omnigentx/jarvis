"""Deterministic confidence (services/memory/confidence.py) — the pure core that
replaces trusting the extractor LLM's arbitrary confidence number."""
from services.memory.confidence import (
    assess_confidence, evidence_supports,
)


# ── evidence_supports: catch fabricated quotes against the snippet ────────────

def test_evidence_supports_verbatim():
    snippet = "user: Tôi đang làm việc tại FPT\nassistant: ok"
    assert evidence_supports("Tôi đang làm việc tại FPT", snippet) is True


def test_evidence_supports_case_and_whitespace_insensitive():
    snippet = "user:   tôi   thích   PHỞ  "
    assert evidence_supports("Tôi thích phở", snippet) is True


def test_evidence_supports_rejects_fabrication():
    snippet = "user: tôi làm ở FPT"
    assert evidence_supports("tôi làm ở Google", snippet) is False


def test_evidence_supports_empty():
    assert evidence_supports("", "anything") is False
    assert evidence_supports("x", "") is False


# ── assess_confidence: verified evidence → tiered by reasoning_type ───────────

def test_direct_verified_is_high():
    v = assess_confidence(reasoning_type="direct", excerpt_ok=True, authority="agent_observed")
    assert v.confidence == 0.9 and v.auto_save_ok is True
    assert v.method == "evidence_alignment_v1:direct"


def test_synthesis_verified_is_medium():
    v = assess_confidence(reasoning_type="synthesis", excerpt_ok=True, authority="agent_observed")
    assert v.confidence == 0.7 and v.auto_save_ok is True


def test_inference_verified_is_lower():
    v = assess_confidence(reasoning_type="inference", excerpt_ok=True, authority="agent_observed")
    assert v.confidence == 0.55


# ── fabricated evidence → distrust + MUST go to human approval ────────────────

def test_unverified_evidence_blocks_autosave():
    v = assess_confidence(reasoning_type="direct", excerpt_ok=False, authority="user_confirmed")
    assert v.auto_save_ok is False          # never auto-save a fabricated claim
    assert v.confidence == 0.4
    assert v.method.endswith(":unverified")


# ── no evidence channel (agent_remember) → authority-derived, varies by source ─

def test_no_evidence_falls_back_to_authority():
    assert assess_confidence(reasoning_type=None, excerpt_ok=None,
                             authority="agent_observed").confidence == 0.6
    assert assess_confidence(reasoning_type=None, excerpt_ok=None,
                             authority="user_confirmed").confidence == 0.9
    assert assess_confidence(reasoning_type=None, excerpt_ok=None,
                             authority="tool_verified").confidence == 0.95
    v = assess_confidence(reasoning_type=None, excerpt_ok=None, authority="agent_observed")
    assert v.auto_save_ok is True and v.method == "authority_default_v1"


def test_unknown_authority_falls_back():
    assert assess_confidence(reasoning_type=None, excerpt_ok=None,
                             authority="bogus").confidence == 0.5
