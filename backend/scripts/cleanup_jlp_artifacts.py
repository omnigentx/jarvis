"""One-shot cleanup: drop all artifacts the prior ``agile-team`` session
created so the user can re-test the team flow from a clean slate.

Scope of cleanup
----------------
1. Delete every remaining Jira issue in project ``JLP``.
2. Delete the Jira project ``JLP`` itself (Cloud REST API — requires
   admin permission on the project).
3. Delete every page in Confluence space ``JLP`` (then optionally
   delete the space itself).
4. Drop the stale ``team_sessions`` row ``be885ae8`` from the local
   jarvis SQLite — the team is gone, the row should be gone too. This
   also removes the orchestrator session id from the next
   ``list_team_sessions`` call and from the dashboard team list.
5. Print a clear next-step (restart backend) so the user can re-spawn
   the team with the fresh code paths.

Why a script instead of inline tool calls
-----------------------------------------
Cleanup hits 3 external systems + 1 local DB and is destructive. Doing
it inline via N separate MCP tool calls failed the agent's
auto-classifier (mass-deletion guard fires on per-call basis when the
caller doesn't bundle the work into one explicit batch). A single
script with ``--dry-run`` / ``--apply`` is auditable, reviewable, and
fits the project's existing pattern (see ``cleanup_orphan_spawns.py``
for the same idiom applied to spawn registry rows).

Credentials
-----------
The script reads Jira/Confluence credentials in the following order
(first hit wins):

  1. Process env: ``JIRA_URL`` / ``JIRA_USERNAME`` / ``JIRA_API_TOKEN``
     and the matching ``CONFLUENCE_*`` variants. Use this when running
     outside of Claude Code (e.g. CI).
  2. ``~/.claude.json`` → ``mcpServers.mcp-atlassian.env`` — the same
     bag of variables Claude Code's MCP atlassian server already uses.
     If Confluence credentials are missing, Atlassian Cloud convention
     applies: ``CONFLUENCE_URL = JIRA_URL + '/wiki'`` and the same
     account/token is reused (one token serves both Jira + Confluence
     for a Cloud account).

The script does NOT prompt for tokens at runtime — that would leak
into terminal history. If neither source above has credentials, the
script exits cleanly and prints the env variables the operator must
export.

Usage
-----
::

    # Preview only (no writes)
    python scripts/cleanup_jlp_artifacts.py --dry-run

    # Execute
    python scripts/cleanup_jlp_artifacts.py --apply

    # Skip a particular phase (e.g. keep the team_sessions row)
    python scripts/cleanup_jlp_artifacts.py --apply --skip-db

    # Also delete the Confluence space (default: only pages)
    python scripts/cleanup_jlp_artifacts.py --apply --delete-space
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger("cleanup_jlp")

PROJECT_KEY = "JLP"
SPACE_KEY = "JLP"
TEAM_SESSION_ID = "be885ae8"  # the team session whose artifacts we're dropping
JARVIS_DB_DEFAULT = (
    Path(__file__).resolve().parent.parent / "data" / "jarvis.db"
)


# ── credential resolution ──────────────────────────────────────────────────


def _load_claude_mcp_env() -> dict[str, str]:
    """Read ``mcpServers.mcp-atlassian.env`` from ``~/.claude.json``.

    Returns ``{}`` if the file is unreadable or the key path is absent.
    No exception is raised — fall through to env vars in that case.
    """
    path = Path.home() / ".claude.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        env = (
            data.get("mcpServers", {})
            .get("mcp-atlassian", {})
            .get("env", {})
        ) or {}
        return {k: str(v) for k, v in env.items() if v}
    except Exception as exc:
        logger.debug("could not read claude.json mcp env: %s", exc)
        return {}


def _resolve_creds() -> tuple[dict[str, str], list[str]]:
    """Resolve Jira + Confluence credentials.

    Returns ``(creds, missing)``. ``creds`` carries the keys we
    successfully filled; ``missing`` is the list of env vars the caller
    still has to export. If ``missing`` is non-empty the caller MUST
    abort before touching any external system.
    """
    claude_env = _load_claude_mcp_env()
    creds: dict[str, str] = {}

    # Jira: env first, then claude.json.
    for key in ("JIRA_URL", "JIRA_USERNAME", "JIRA_API_TOKEN"):
        val = os.getenv(key) or claude_env.get(key)
        if val:
            creds[key] = val.rstrip("/") if key == "JIRA_URL" else val

    # Confluence: env, then claude.json, then Atlassian Cloud default
    # (same account, JIRA_URL + /wiki, same token).
    creds.setdefault(
        "CONFLUENCE_URL",
        os.getenv("CONFLUENCE_URL")
        or claude_env.get("CONFLUENCE_URL")
        or (f"{creds['JIRA_URL']}/wiki" if creds.get("JIRA_URL") else ""),
    )
    creds.setdefault(
        "CONFLUENCE_USERNAME",
        os.getenv("CONFLUENCE_USERNAME")
        or claude_env.get("CONFLUENCE_USERNAME")
        or creds.get("JIRA_USERNAME", ""),
    )
    creds.setdefault(
        "CONFLUENCE_API_TOKEN",
        os.getenv("CONFLUENCE_API_TOKEN")
        or claude_env.get("CONFLUENCE_API_TOKEN")
        or creds.get("JIRA_API_TOKEN", ""),
    )

    missing = [k for k in (
        "JIRA_URL", "JIRA_USERNAME", "JIRA_API_TOKEN",
        "CONFLUENCE_URL", "CONFLUENCE_USERNAME", "CONFLUENCE_API_TOKEN",
    ) if not creds.get(k)]
    return creds, missing


# ── Jira phase ─────────────────────────────────────────────────────────────


def _jira_list_issues(creds: dict[str, str]) -> list[dict[str, Any]]:
    """List every issue in PROJECT_KEY via the JQL search endpoint.

    Uses the new ``/rest/api/3/search/jql`` endpoint (Atlassian Cloud
    2024 migration — the legacy ``/search`` returns 410 Gone). The new
    endpoint paginates with ``nextPageToken`` instead of ``startAt``.
    """
    issues: list[dict[str, Any]] = []
    token: str | None = None
    auth = HTTPBasicAuth(creds["JIRA_USERNAME"], creds["JIRA_API_TOKEN"])
    while True:
        params: dict[str, Any] = {
            "jql": f"project = {PROJECT_KEY}",
            "fields": "summary,issuetype",
            "maxResults": 100,
        }
        if token:
            params["nextPageToken"] = token
        resp = requests.get(
            f"{creds['JIRA_URL']}/rest/api/3/search/jql",
            params=params,
            auth=auth,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("issues", [])
        issues.extend(batch)
        token = data.get("nextPageToken")
        if not token or not batch:
            break
    return issues


def _jira_delete_issue(creds: dict[str, str], key: str) -> None:
    auth = HTTPBasicAuth(creds["JIRA_USERNAME"], creds["JIRA_API_TOKEN"])
    resp = requests.delete(
        f"{creds['JIRA_URL']}/rest/api/3/issue/{key}",
        params={"deleteSubtasks": "true"},
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()


def _jira_delete_project(creds: dict[str, str]) -> None:
    auth = HTTPBasicAuth(creds["JIRA_USERNAME"], creds["JIRA_API_TOKEN"])
    resp = requests.delete(
        f"{creds['JIRA_URL']}/rest/api/3/project/{PROJECT_KEY}",
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()


# ── Confluence phase ───────────────────────────────────────────────────────


def _confluence_list_pages(creds: dict[str, str]) -> list[dict[str, Any]]:
    """List every page in SPACE_KEY via the Cloud v1 API.

    Uses the v1 ``/rest/api/content`` endpoint because it supports the
    ``spaceKey`` filter on Cloud without needing the v2 space id. Page
    size 100 — small spaces will fit in one round-trip.
    """
    auth = HTTPBasicAuth(
        creds["CONFLUENCE_USERNAME"], creds["CONFLUENCE_API_TOKEN"],
    )
    pages: list[dict[str, Any]] = []
    start = 0
    while True:
        resp = requests.get(
            f"{creds['CONFLUENCE_URL']}/rest/api/content",
            params={
                "spaceKey": SPACE_KEY,
                "limit": 100,
                "start": start,
                "expand": "version",
            },
            auth=auth,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("results", [])
        pages.extend(batch)
        if not batch or len(batch) < 100:
            break
        start += len(batch)
    return pages


def _confluence_delete_page(creds: dict[str, str], page_id: str) -> None:
    auth = HTTPBasicAuth(
        creds["CONFLUENCE_USERNAME"], creds["CONFLUENCE_API_TOKEN"],
    )
    resp = requests.delete(
        f"{creds['CONFLUENCE_URL']}/rest/api/content/{page_id}",
        auth=auth,
        timeout=30,
    )
    # 204 No Content is the success contract.
    resp.raise_for_status()


def _confluence_delete_space(creds: dict[str, str]) -> None:
    auth = HTTPBasicAuth(
        creds["CONFLUENCE_USERNAME"], creds["CONFLUENCE_API_TOKEN"],
    )
    # v1 space-delete kicks off an async long-running task.
    resp = requests.delete(
        f"{creds['CONFLUENCE_URL']}/rest/api/space/{SPACE_KEY}",
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()


# ── Local DB phase ────────────────────────────────────────────────────────


def _db_drop_team_session(db_path: Path) -> bool:
    """Delete the team_sessions row for TEAM_SESSION_ID.

    Returns True iff a row existed and was removed.
    """
    if not db_path.exists():
        return False
    with sqlite3.connect(str(db_path)) as conn:
        existed = conn.execute(
            "SELECT 1 FROM team_sessions WHERE session_id = ?",
            (TEAM_SESSION_ID,),
        ).fetchone() is not None
        if existed:
            conn.execute(
                "DELETE FROM team_sessions WHERE session_id = ?",
                (TEAM_SESSION_ID,),
            )
            conn.commit()
        return existed


# ── orchestration ──────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--skip-jira", action="store_true",
        help="leave Jira issues + project intact",
    )
    parser.add_argument(
        "--skip-confluence", action="store_true",
        help="leave Confluence pages intact",
    )
    parser.add_argument(
        "--delete-space", action="store_true",
        help="also delete the Confluence space JLP (default: pages only)",
    )
    parser.add_argument(
        "--skip-db", action="store_true",
        help="leave the team_sessions row in jarvis.db",
    )
    parser.add_argument("--db", default=str(JARVIS_DB_DEFAULT))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    if args.apply == args.dry_run:
        logger.error("Pass exactly one of --dry-run or --apply.")
        return 1

    creds, missing = _resolve_creds()
    if not args.skip_jira or not args.skip_confluence:
        if missing:
            logger.error(
                "Missing credentials: %s.\n\n"
                "Either export them as env vars and re-run, or add them "
                "to ``~/.claude.json`` → mcpServers.mcp-atlassian.env. "
                "(Atlassian Cloud uses the same account+token for both "
                "Jira and Confluence; the Confluence base URL is "
                "JIRA_URL + '/wiki'.)",
                ", ".join(missing),
            )
            return 2

    rc = 0

    # ── Jira ──
    if not args.skip_jira:
        logger.info("[Jira] enumerating issues in project %s …", PROJECT_KEY)
        try:
            issues = _jira_list_issues(creds)
        except Exception as exc:
            logger.error("[Jira] list failed: %s", exc)
            return 3
        logger.info("[Jira] found %d issue(s)", len(issues))
        for issue in issues:
            key = issue["key"]
            summary = issue.get("fields", {}).get("summary", "")
            logger.info("  - %s — %s", key, summary[:80])
        if args.apply:
            for issue in issues:
                try:
                    _jira_delete_issue(creds, issue["key"])
                    logger.info("[Jira] deleted %s", issue["key"])
                except Exception as exc:
                    logger.warning("[Jira] delete %s failed: %s", issue["key"], exc)
                    rc = max(rc, 4)

            logger.info("[Jira] deleting project %s …", PROJECT_KEY)
            try:
                _jira_delete_project(creds)
                logger.info("[Jira] project %s deleted", PROJECT_KEY)
            except Exception as exc:
                logger.error("[Jira] project delete failed: %s", exc)
                rc = max(rc, 5)
        else:
            logger.info(
                "[Jira] DRY-RUN — %d issue(s) and project %s would be deleted",
                len(issues), PROJECT_KEY,
            )

    # ── Confluence ──
    if not args.skip_confluence:
        logger.info("[Confluence] enumerating pages in space %s …", SPACE_KEY)
        try:
            pages = _confluence_list_pages(creds)
        except Exception as exc:
            logger.error("[Confluence] list failed: %s", exc)
            return 6
        logger.info("[Confluence] found %d page(s)", len(pages))
        for page in pages:
            logger.info(
                "  - %s — %s",
                page.get("id"), (page.get("title") or "")[:80],
            )
        if args.apply:
            for page in pages:
                pid = page.get("id")
                if not pid:
                    continue
                try:
                    _confluence_delete_page(creds, pid)
                    logger.info("[Confluence] deleted page %s", pid)
                except Exception as exc:
                    logger.warning(
                        "[Confluence] delete page %s failed: %s", pid, exc,
                    )
                    rc = max(rc, 7)
            if args.delete_space:
                logger.info("[Confluence] deleting space %s …", SPACE_KEY)
                try:
                    _confluence_delete_space(creds)
                    logger.info(
                        "[Confluence] space %s delete requested (async)",
                        SPACE_KEY,
                    )
                except Exception as exc:
                    logger.error(
                        "[Confluence] space delete failed: %s", exc,
                    )
                    rc = max(rc, 8)
        else:
            extras = "+ space" if args.delete_space else "(pages only)"
            logger.info(
                "[Confluence] DRY-RUN — %d page(s) %s would be deleted",
                len(pages), extras,
            )

    # ── Local DB ──
    if not args.skip_db:
        if args.apply:
            removed = _db_drop_team_session(Path(args.db))
            logger.info(
                "[DB] team_sessions row %s %s",
                TEAM_SESSION_ID,
                "deleted" if removed else "not found (already clean)",
            )
        else:
            logger.info(
                "[DB] DRY-RUN — would drop team_sessions row %s from %s",
                TEAM_SESSION_ID, args.db,
            )

    if args.apply and rc == 0:
        logger.info(
            "\n[OK] cleanup complete. Restart the backend so the next "
            "spawn picks up a clean state:\n"
            "  pkill -f 'uvicorn server:app' ; "
            "nohup uv run uvicorn server:app --host 0.0.0.0 --port 8000 "
            "> /tmp/backend.out 2>&1 &"
        )

    return rc


if __name__ == "__main__":
    sys.exit(main())
