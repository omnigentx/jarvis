"""One-time reconciliation: remove orphan ``spawn_registry`` rows where a
team-managed agent was registered with a non-distinct identity.

Why this script exists
----------------------
Incident 2026-05-17: a silent fallback in ``isolated_spawner.run_isolated_agent_background``
(``agent_name=agent_name or role or "agent"``) substituted the role string
when a caller forgot to pass ``agent_name``. The team-managed PM was
registered twice — once as ``"Robin [PM]"`` (correct) and once as
``"pm"`` (just the role). The dashboard, treating ``agent_name`` as the
unique identity, rendered two separate cards for one logical agent.

The runtime validator in :class:`SpawnRegistry` now rejects future writes
of this shape. This script cleans up the historical rows that landed
BEFORE the validator was deployed.

It is intentionally Python (matches repo style), idempotent (safe to
re-run), and supports ``--dry-run`` so an operator can preview the diff
before deleting.

After this script lands and runs once, it should NOT need to run again —
the runtime validator prevents the bug class from recurring. Keep the
script in-tree as documentation of the recovery operation.

Usage::

    # Preview which rows would be deleted (no DB writes)
    python scripts/cleanup_orphan_spawns.py --dry-run

    # Actually delete
    python scripts/cleanup_orphan_spawns.py --apply

    # Filter to a specific team
    python scripts/cleanup_orphan_spawns.py --apply --team jarvis-landing-jira-confluence
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger("cleanup_orphan_spawns")

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "jarvis.db"


def find_orphans(db_path: Path, team: str | None = None) -> list[dict]:
    """Find rows where team_name is set AND agent_name equals role (or empty)."""
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT run_id, data_json FROM spawn_registry").fetchall()
    finally:
        conn.close()

    orphans: list[dict] = []
    for run_id, data_json in rows:
        try:
            rec = json.loads(data_json)
        except (json.JSONDecodeError, TypeError):
            continue
        tn = rec.get("team_name") or ""
        role = rec.get("role") or ""
        agent_name = rec.get("agent_name") or ""
        if not tn:
            continue  # ad-hoc spawn — out of scope
        if team and tn != team:
            continue
        if not agent_name or agent_name == role:
            orphans.append({
                "run_id": run_id,
                "team_name": tn,
                "role": role,
                "agent_name": agent_name,
                "status": rec.get("status"),
                "pid": rec.get("pid"),
                "started_at": rec.get("started_at"),
                # Surface the ORIGINAL config's agent_name — usually correct,
                # which proves the orphan is "same logical agent, wrong row".
                "original_config_agent_name": (
                    (rec.get("original_config") or {}).get("agent_name")
                ),
            })
    return orphans


def delete_run_ids(db_path: Path, run_ids: list[str]) -> int:
    if not run_ids:
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        placeholders = ",".join("?" for _ in run_ids)
        cur = conn.execute(
            f"DELETE FROM spawn_registry WHERE run_id IN ({placeholders})",
            run_ids,
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB), help="path to jarvis.db")
    parser.add_argument("--team", default=None, help="restrict cleanup to one team_name")
    parser.add_argument("--dry-run", action="store_true", help="preview only, no writes")
    parser.add_argument("--apply", action="store_true", help="actually delete")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    if args.apply == args.dry_run:
        # Either both set or neither — force the operator to pick one.
        logger.error("Specify exactly one of --apply or --dry-run.")
        return 1

    db_path = Path(args.db)
    try:
        orphans = find_orphans(db_path, team=args.team)
    except FileNotFoundError:
        logger.error("DB not found: %s", db_path)
        return 2

    if not orphans:
        logger.info("[OK] no orphan spawn_registry rows found")
        return 0

    logger.info("Found %d orphan row(s):", len(orphans))
    for o in orphans:
        logger.info(
            "  - run_id=%s team=%s role=%s agent_name=%r (orig_config.agent_name=%r) status=%s pid=%s",
            o["run_id"], o["team_name"], o["role"], o["agent_name"],
            o["original_config_agent_name"], o["status"], o["pid"],
        )

    if args.dry_run:
        logger.info("[DRY-RUN] no rows deleted. Re-run with --apply to delete.")
        return 0

    run_ids = [o["run_id"] for o in orphans]
    deleted = delete_run_ids(db_path, run_ids)
    logger.info("[OK] deleted %d row(s)", deleted)
    logger.info(
        "Reload the dashboard — the stale agent card should be gone. "
        "Future writes of this shape are blocked by SpawnRegistry validator."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
