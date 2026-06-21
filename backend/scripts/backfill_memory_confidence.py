"""One-time backfill: recover real per-memory ``confidence`` from the candidate
that produced each memory.

Why this script exists
----------------------
Until the fix in ``candidate_service._persist_from_candidate``, confidence never
reached a memory record: ``create_memory`` was called without a ``confidence=``
argument, so every memory defaulted to ``0.5`` regardless of what the extractor
or compactor had determined. Two consequences:

1. The chat "memories used" debug chip showed a flat ``conf 0.5`` everywhere
   (looked hardcoded).
2. The relevance policy's ``(confidence - 0.5)`` rank-boost (``fusion.py``) was a
   permanent no-op — confidence never influenced ranking.

The runtime fix threads confidence end-to-end, so memories captured AFTER it get
real values. This script repairs memories captured BEFORE it, WITHOUT re-running
any LLM: the originating candidate still holds the real value —
``payload_json["confidence"]`` for the extracted/fast lane, or the
``confidence`` column for the compaction lane. We match memory→candidate by
(owner, content) and lift the recovered value.

It is idempotent (only touches memories still at the ``0.5`` default for which a
DIFFERENT recovered value exists) and supports ``--dry-run`` for preview.
Candidates with no recoverable value (e.g. the ``agent_remember`` tool lane never
stored one) are left untouched — we never invent a number.

Usage::

    python scripts/backfill_memory_confidence.py --dry-run
    python scripts/backfill_memory_confidence.py --apply
    python scripts/backfill_memory_confidence.py --apply --db /path/to/jarvis.db
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger("backfill_memory_confidence")

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "jarvis.db"
STALE_DEFAULT = 0.5


def _recovered_confidence(payload_json: str, column_conf: float | None) -> float | None:
    """The real confidence the candidate carried, or None if it never had one.
    Payload (extracted/fast lane) wins over the column (compaction lane)."""
    try:
        payload = json.loads(payload_json or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {}
    pc = payload.get("confidence")
    if isinstance(pc, (int, float)):
        return float(pc)
    if isinstance(column_conf, (int, float)) and column_conf != STALE_DEFAULT:
        return float(column_conf)
    return None


def plan_backfill(db_path: Path) -> list[dict]:
    """Memories still at the 0.5 default whose originating candidate holds a
    different real confidence. Match by (owner, content)."""
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # candidate lookup keyed by (owner, content) → best recoverable confidence.
        cand_conf: dict[tuple[str, str], float] = {}
        for r in conn.execute(
            "SELECT owner_agent_name, confidence, payload_json FROM memory_candidates"
        ):
            try:
                content = (json.loads(r["payload_json"] or "{}")).get("content", "")
            except (json.JSONDecodeError, TypeError):
                continue
            rec = _recovered_confidence(r["payload_json"], r["confidence"])
            if rec is None or not content:
                continue
            key = (r["owner_agent_name"], content)
            # Collision assumption: we match memory→candidate by (owner, content)
            # only — there's no candidate_id FK on memory_records. Two DISTINCT
            # memories with identical content+owner would both receive the max()
            # recovered confidence here. Acceptable: this is a one-time, idempotent
            # (--dry-run-able) repair, and identical content+owner is itself the
            # exact-dedup key, so genuine distinct duplicates shouldn't coexist.
            cand_conf[key] = max(cand_conf.get(key, 0.0), rec)

        plan: list[dict] = []
        for r in conn.execute(
            "SELECT id, owner_agent_name, content, confidence FROM memory_records "
            "WHERE status='active'"
        ):
            if r["confidence"] != STALE_DEFAULT:
                continue  # already has a real value — idempotent skip
            rec = cand_conf.get((r["owner_agent_name"], r["content"]))
            if rec is None or rec == STALE_DEFAULT:
                continue  # nothing better to apply
            plan.append({
                "id": r["id"], "owner": r["owner_agent_name"],
                "content": r["content"][:50], "old": r["confidence"], "new": rec,
            })
        return plan
    finally:
        conn.close()


def apply_backfill(db_path: Path, plan: list[dict]) -> int:
    if not plan:
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "UPDATE memory_records SET confidence=? WHERE id=?",
            [(p["new"], p["id"]) for p in plan],
        )
        conn.commit()
        return conn.total_changes
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Preview, no DB writes")
    g.add_argument("--apply", action="store_true", help="Apply the backfill")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"DB path (default {DEFAULT_DB})")
    args = ap.parse_args(argv)

    plan = plan_backfill(args.db)
    if not plan:
        logger.info("Nothing to backfill — no stale-0.5 memory has a recoverable confidence.")
        return 0

    logger.info("%d memory record(s) to update:", len(plan))
    for p in plan:
        logger.info("  %.2f → %.2f  [%s] %s", p["old"], p["new"], p["owner"], p["content"])

    if args.dry_run:
        logger.info("\n(dry-run — no changes written. Re-run with --apply.)")
        return 0

    n = apply_backfill(args.db, plan)
    logger.info("\nApplied: %d row(s) updated.", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
