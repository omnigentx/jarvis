"""Deterministic, evidence-grounded confidence for captured memories — NO LLM.

WHY deterministic (and not an LLM verifier):
A memory's confidence must be predictable, testable, debuggable, and free to
compute. We derive it from signals the BACKEND can verify, NOT from a number the
extractor LLM emits — that number has no rubric and is therefore arbitrary
(why was "name" 1.0 and "job" 0.95? nothing). A second LLM "verifier" would only
trade that arbitrariness for non-determinism + cost; a pure function is the
higher-quality choice for a system we maintain and scale.

Single source of truth for "how confident are we": the formula lives ONLY here,
so it evolves in one place. Every write records ``method`` in
``MemoryVersion.metadata_json`` so a later formula change stays auditable and
migratable.

Signals:
- ``reasoning_type`` — how the memory relates to its evidence: a direct restatement
  vs multi-turn synthesis vs looser inference (emitted by the extractor).
- ``excerpt_ok`` — did the BACKEND find the claimed evidence verbatim in the source
  we showed the LLM? ``True`` = verified, ``False`` = cited evidence absent from
  the source (fabricated → distrust + human review), ``None`` = this lane has no
  evidence channel (e.g. the ``agent_remember`` tool) → fall back to authority.
- ``authority`` — trust class of the source (user_confirmed > agent_observed > …).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

CONFIDENCE_METHOD = "evidence_alignment_v1"

# Verified-evidence base, by how directly the memory restates that evidence.
REASONING_BASE = {"direct": 0.9, "synthesis": 0.7, "inference": 0.55}
_REASONING_FALLBACK = 0.6
# When a lane has NO evidence to verify (agent_remember), derive from authority.
AUTHORITY_BASE = {
    "tool_verified": 0.95, "user_confirmed": 0.9, "external_document": 0.7,
    "agent_observed": 0.6, "reported_by_agent": 0.6, "inferred": 0.5,
}
_AUTHORITY_FALLBACK = 0.5
# Evidence was CLAIMED but not found in the source → the LLM fabricated it.
_UNVERIFIED_CONFIDENCE = 0.4


@dataclass(frozen=True)
class ConfidenceVerdict:
    confidence: float
    method: str
    auto_save_ok: bool   # False → must route to human approval, never auto-persist


def assess_confidence(*, reasoning_type: Optional[str], excerpt_ok: Optional[bool],
                      authority: str) -> ConfidenceVerdict:
    """Map verified signals → a confidence + its method tag + whether auto-save is
    allowed. Pure and total: same inputs always give the same verdict."""
    rt = (reasoning_type or "").lower().strip()
    if excerpt_ok is False:
        # Cited evidence not present in the source → distrust; a human must vet it
        # before it can be stored (never silently auto-save a fabricated claim).
        return ConfidenceVerdict(_UNVERIFIED_CONFIDENCE,
                                 f"{CONFIDENCE_METHOD}:unverified", auto_save_ok=False)
    if excerpt_ok is True:
        base = REASONING_BASE.get(rt, _REASONING_FALLBACK)
        return ConfidenceVerdict(base, f"{CONFIDENCE_METHOD}:{rt or 'unknown'}",
                                 auto_save_ok=True)
    # excerpt_ok is None → no evidence channel for this lane → authority-derived.
    base = AUTHORITY_BASE.get((authority or "").lower().strip(), _AUTHORITY_FALLBACK)
    return ConfidenceVerdict(base, "authority_default_v1", auto_save_ok=True)


def evidence_supports(excerpt: str, source_text: str) -> bool:
    """True if ``excerpt`` appears (normalized) in ``source_text`` — the snippet we
    showed the LLM. Catches fabricated evidence WITHOUT needing an id-addressable
    message store: the proof we keep is the verbatim excerpt itself (persisted in
    ``memory_sources``), which outlives any ephemeral message buffer."""
    if not excerpt or not source_text:
        return False
    return _norm(excerpt) in _norm(source_text)


def _norm(s: str) -> str:
    # Case + whitespace insensitive; accents preserved (we want a faithful quote).
    return " ".join(s.lower().split())
