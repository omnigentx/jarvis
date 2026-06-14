"""Build + enforce retrieval budgets (spec §7.4). Budgets come ONLY from the
mode preset + settings overrides; the orchestrator enforces them.
"""
from __future__ import annotations

from dataclasses import replace

from services.retrieval.contracts import DEFAULT_BUDGETS, RetrievalBudget, RetrievalMode


def build_budget(mode: str, *, evidence_token_budget: int | None = None) -> RetrievalBudget:
    """Return the budget for ``mode`` (falls back to balanced), with the
    user's evidence-token override applied when provided."""
    preset = DEFAULT_BUDGETS.get(mode, DEFAULT_BUDGETS[RetrievalMode.BALANCED.value])
    budget = replace(preset)  # copy — never mutate the shared preset
    if evidence_token_budget is not None:
        budget.max_evidence_tokens = evidence_token_budget
    return budget
