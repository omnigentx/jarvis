"""Evidence ledger (spec §10). Tracks evidence already introduced into a
working context so the router can skip retrieval when sufficient evidence is
present, and so the same evidence is never injected twice. Survives
compaction; evidence dropped by compaction is simply re-retrieved.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from services.retrieval.contracts import Evidence


@dataclass
class LedgerEntry:
    evidence_id: str
    record_id: str
    introduced_at_turn: int
    last_used_at_turn: int


@dataclass
class EvidenceLedger:
    entries: dict[str, LedgerEntry] = field(default_factory=dict)

    def has(self, evidence_id: str) -> bool:
        return evidence_id in self.entries

    def add(self, ev: Evidence, *, turn: int) -> None:
        self.entries[ev.evidence_id] = LedgerEntry(
            evidence_id=ev.evidence_id, record_id=ev.record_id,
            introduced_at_turn=turn, last_used_at_turn=turn,
        )

    def dedup(self, evidence: list[Evidence], *, turn: int) -> list[Evidence]:
        """Drop evidence already in the ledger; touch the survivors' last-used
        turn for the ones we keep."""
        fresh: list[Evidence] = []
        for ev in evidence:
            if ev.evidence_id in self.entries:
                self.entries[ev.evidence_id].last_used_at_turn = turn
                continue
            fresh.append(ev)
        return fresh

    def record_ids(self) -> set[str]:
        return {e.record_id for e in self.entries.values()}
