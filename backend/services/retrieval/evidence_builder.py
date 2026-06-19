"""Trim fused evidence to the budget (item + token caps) before injection."""
from __future__ import annotations

from services.retrieval.contracts import Evidence, RetrievalBudget


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def build_evidence(fused: list[Evidence], budget: RetrievalBudget) -> tuple[list[Evidence], int]:
    """Return (selected evidence, total tokens) within the budget's item and
    token caps. Highest-ranked first; stop when either cap is reached."""
    selected: list[Evidence] = []
    total = 0
    for ev in fused:
        if len(selected) >= budget.max_evidence_items:
            break
        cost = estimate_tokens(ev.excerpt)
        if selected and total + cost > budget.max_evidence_tokens:
            break
        selected.append(ev)
        total += cost
    return selected, total
