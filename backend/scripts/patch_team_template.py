"""Phase 0 throwaway: patch a running team's template directly in DB.

Why this exists
---------------
`team_sessions.template` is a JSON snapshot frozen at team creation time. When
`team_templates/*.yaml` is updated AFTER a team is running, the running team
does NOT pick up the change (e.g. 2026-05-17 incident: QE missing playwright).

This script is the manual fix until Phase 1 lands the proper edit endpoints.
It is intentionally Python (matches repo style) and idempotent so it can be
re-run safely. Every edit writes an audit row to ``team_template_history`` —
the same table Phase 1's REST API will use, so audit history stays continuous
across the migration.

Usage
-----
    python scripts/patch_team_template.py \\
        --session-id be885ae8 --role qe \\
        --add-servers playwright \\
        --comment "phase 0 manual fix — yaml had playwright, frozen DB didn't"

    # multiple ops in one call
    python scripts/patch_team_template.py \\
        --session-id be885ae8 --role dev \\
        --add-servers playwright \\
        --remove-servers some-old-server \\
        --dry-run

The script:
  1. Reads ``team_sessions.data_json`` for the session
  2. Walks to ``template.roles[role].servers``
  3. Applies add/remove ops (de-duplicated, order preserved)
  4. CREATE TABLE IF NOT EXISTS ``team_template_history`` (matches Phase 1 schema)
  5. INSERT audit row (before_json, after_json, source='phase0-script')
  6. UPDATE ``team_sessions`` atomically (single transaction)

The script does NOT kill running agent processes. After patching, the next
agent spawn (auto-resume / inject / restart) reads fresh from DB and picks up
the new server list. To force immediate effect, kill the agent process tree
manually and re-inject.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

logger = logging.getLogger("patch_team_template")

# Match Phase 1 schema exactly so history persists across the migration.
AUDIT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS team_template_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id VARCHAR(100) NOT NULL,
    role VARCHAR(100),
    field VARCHAR(100),
    before_json TEXT,
    after_json TEXT,
    source VARCHAR(50) NOT NULL,
    edited_by VARCHAR(100),
    edited_at FLOAT NOT NULL,
    comment TEXT
)
"""
AUDIT_IDX_SESSION = "CREATE INDEX IF NOT EXISTS idx_tth_session_id ON team_template_history(session_id)"
AUDIT_IDX_TIME = "CREATE INDEX IF NOT EXISTS idx_tth_edited_at ON team_template_history(edited_at)"

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "jarvis.db"


def _apply_server_ops(servers: list[str], add: list[str], remove: list[str]) -> list[str]:
    """Return new server list. Preserves order, de-duplicates, idempotent."""
    out = list(servers)
    for s in add:
        if s not in out:
            out.append(s)
    out = [s for s in out if s not in remove]
    return out


def patch(
    db_path: Path,
    session_id: str,
    role: str,
    add_servers: list[str],
    remove_servers: list[str],
    comment: str,
    edited_by: str = "system",
    source: str = "phase0-script",
    dry_run: bool = False,
) -> int:
    """Apply the patch. Returns 0 on success, non-zero on error."""
    if not db_path.exists():
        logger.error("DB file not found: %s", db_path)
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        cur = conn.cursor()
        cur.execute("SELECT data_json FROM team_sessions WHERE session_id=?", (session_id,))
        row = cur.fetchone()
        if not row:
            logger.error("No team_sessions row for session_id=%r", session_id)
            return 3

        data = json.loads(row[0])
        roles = data.get("template", {}).get("roles", {})
        if role not in roles:
            logger.error(
                "Role %r not found in team template. Available: %s",
                role, sorted(roles.keys()),
            )
            return 4

        before_servers = list(roles[role].get("servers", []))
        after_servers = _apply_server_ops(before_servers, add_servers, remove_servers)

        if before_servers == after_servers:
            logger.info(
                "[NOOP] %s/%s servers already in desired state: %s",
                session_id, role, before_servers,
            )
            return 0

        logger.info("[%s/%s] servers", session_id, role)
        logger.info("  BEFORE: %s", before_servers)
        logger.info("  AFTER:  %s", after_servers)
        added = [s for s in after_servers if s not in before_servers]
        removed = [s for s in before_servers if s not in after_servers]
        if added:
            logger.info("  +ADDED: %s", added)
        if removed:
            logger.info("  -REMOVED: %s", removed)

        if dry_run:
            logger.info("[DRY-RUN] no changes written")
            return 0

        # Apply patch
        roles[role]["servers"] = after_servers
        new_json = json.dumps(data, ensure_ascii=False)

        # Ensure audit table exists (idempotent)
        cur.execute(AUDIT_TABLE_DDL)
        cur.execute(AUDIT_IDX_SESSION)
        cur.execute(AUDIT_IDX_TIME)

        now = time.time()
        cur.execute(
            """
            INSERT INTO team_template_history
              (session_id, role, field, before_json, after_json,
               source, edited_by, edited_at, comment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, role, "servers",
                json.dumps(before_servers, ensure_ascii=False),
                json.dumps(after_servers, ensure_ascii=False),
                source, edited_by, now, comment,
            ),
        )
        audit_id = cur.lastrowid

        cur.execute(
            "UPDATE team_sessions SET data_json=? WHERE session_id=?",
            (new_json, session_id),
        )

        conn.commit()
        logger.info("[OK] applied. audit_id=%d, edited_at=%.3f", audit_id, now)
        logger.info(
            "Next spawn (auto-resume / inject / restart) of role=%r in this "
            "session will pick up the new server list. To force-apply now: "
            "kill the running agent process tree.",
            role,
        )
        return 0
    except Exception as exc:
        conn.rollback()
        logger.error("[ERROR] %s", exc, exc_info=True)
        return 5
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--role", required=True, help="role key in template, e.g. 'qe', 'dev'")
    parser.add_argument("--add-servers", nargs="*", default=[], help="server names to ensure present")
    parser.add_argument("--remove-servers", nargs="*", default=[], help="server names to remove")
    parser.add_argument("--comment", default="", help="audit-log comment explaining the edit")
    parser.add_argument("--edited-by", default="system")
    parser.add_argument("--source", default="phase0-script")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="path to jarvis.db")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    if not args.add_servers and not args.remove_servers:
        logger.error("Specify at least one of --add-servers / --remove-servers")
        return 1

    return patch(
        db_path=Path(args.db),
        session_id=args.session_id,
        role=args.role,
        add_servers=args.add_servers,
        remove_servers=args.remove_servers,
        comment=args.comment,
        edited_by=args.edited_by,
        source=args.source,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
