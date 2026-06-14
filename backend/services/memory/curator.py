"""Memory Curator (spec §11.4). Called ONLY for the hard cases: a candidate
that conflicts with active memory, has ambiguous scope/type, should merge, or
needs sensitivity classification. Returns a STRUCTURED decision and has NO
database tools — MemoryService/candidate_service execute the decision after
re-validating.

The LLM call is injected (``llm_fn``) so this is testable without a live model
and so the configured curator model/provider (settings) is supplied by the
caller. Default model is low-cost.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("memory.curator")

# decision actions
CREATE = "create"
MERGE = "merge"
SUPERSEDE = "supersede"
REJECT = "reject"
NEEDS_APPROVAL = "needs_approval"

_VALID_ACTIONS = {CREATE, MERGE, SUPERSEDE, REJECT, NEEDS_APPROVAL}


@dataclass
class CuratorDecision:
    action: str
    reason: str = ""
    merged_content: Optional[str] = None
    supersedes_id: Optional[str] = None

    @classmethod
    def from_json(cls, text: str) -> "CuratorDecision":
        data = json.loads(text)
        action = data.get("action")
        if action not in _VALID_ACTIONS:
            raise ValueError(f"invalid curator action {action!r}")
        return cls(action=action, reason=data.get("reason", ""),
                   merged_content=data.get("merged_content"),
                   supersedes_id=data.get("supersedes_id"))


_PROMPT = """You are a memory curator. Decide what to do with a PROPOSED memory
given any CONFLICTING existing memories. Respond with ONLY JSON:
{{"action": "create|merge|supersede|reject|needs_approval",
  "reason": "...", "merged_content": "...", "supersedes_id": "..."}}

PROPOSED:
{candidate}

CONFLICTS:
{conflicts}
"""


class MemoryCurator:
    def __init__(self, llm_fn: Callable[[str], str]):
        """``llm_fn`` takes a prompt string and returns the model's text."""
        self._llm = llm_fn

    def decide(self, candidate: dict, conflicts: list[dict]) -> CuratorDecision:
        prompt = _PROMPT.format(
            candidate=json.dumps(candidate, ensure_ascii=False),
            conflicts=json.dumps(conflicts, ensure_ascii=False),
        )
        raw = self._llm(prompt)
        try:
            return CuratorDecision.from_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            # A malformed curator response must never silently create memory.
            logger.warning("[MEMORY] curator returned unparseable decision: %s", exc)
            return CuratorDecision(action=NEEDS_APPROVAL,
                                   reason="curator response unparseable")
