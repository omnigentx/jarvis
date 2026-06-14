"""Deterministic retrieval router (spec §7). ALL plain code, ONE module, no
LLM. Decides the retrieval level and what to target from bilingual lexicon
signals + identifier regexes + ledger/agent state.

  Level 0: no retrieval (the dominant path)
  Level 1: deterministic fast hybrid retrieval
  Level 2: bounded agentic retrieval (only when Level 1 is weak / requested)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from helpers.memory_triggers import (
    TARGET_EXTERNAL,
    classify_targets,
    has_exact_identifier,
)

LEVEL_NONE = 0
LEVEL_FAST = 1
LEVEL_AGENTIC = 2


@dataclass
class RouteDecision:
    level: int
    reason: str
    targets: set[str] = field(default_factory=set)
    bm25_first: bool = False


def decide_initial(
    query: str,
    *,
    agent_requested: bool = False,
    continuing_tool_loop: bool = False,
    ledger_has_sufficient: bool = False,
    lexicon_overrides: dict | None = None,
    gate_targets: set[str] | None = None,
) -> RouteDecision:
    """Pick the initial retrieval level for a turn. Level 0 must be the common
    outcome — only escalate to Level 1 on a concrete signal.

    ``gate_targets`` is the MULTILINGUAL signal from the embedding intent gate
    (services/retrieval/intent_gate.py). When provided it is the primary target
    source; otherwise the English substring lexicon is the fallback. The
    identifier regex (language-agnostic) always applies.
    """
    # Required evidence already present → no search (spec §7.1).
    if ledger_has_sufficient:
        return RouteDecision(LEVEL_NONE, "evidence already in ledger")
    # Mid tool-loop continuation → don't interrupt with retrieval.
    if continuing_tool_loop and not agent_requested:
        return RouteDecision(LEVEL_NONE, "continuing current tool loop")

    ident = has_exact_identifier(query)
    targets = (gate_targets if gate_targets is not None
               else classify_targets(query, overrides=lexicon_overrides))
    signal = "intent gate" if gate_targets is not None else "lexicon signal"

    # The agent explicitly asked for memory → always retrieve.
    if agent_requested:
        return RouteDecision(LEVEL_FAST, "agent requested retrieval",
                             targets=targets - {TARGET_EXTERNAL}, bm25_first=ident)

    # Fresh external info is NOT memory — a lone external signal stays Level 0.
    real_targets = targets - {TARGET_EXTERNAL}
    if not real_targets and not ident:
        return RouteDecision(LEVEL_NONE, "no historical-knowledge signal")

    reason = "identifier match" if ident and not real_targets else signal
    return RouteDecision(LEVEL_FAST, reason, targets=real_targets, bm25_first=ident)


def should_escalate(
    *,
    weak: bool,
    mode: str,
    deep_requested: bool,
    high_risk: bool,
    rounds_used: int,
    max_rounds: int,
) -> bool:
    """Decide Level 1 → Level 2. Bounded by the mode's corrective-round budget;
    `weak` comes from the quality gate."""
    if rounds_used >= max_rounds:
        return False
    if max_rounds <= 0:           # economical: planner off
        return False
    return weak or deep_requested or high_risk
