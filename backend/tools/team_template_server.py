"""MCP tool server — exposes team-template edit/inspect to Jarvis agents.

Two surfaces:

  Factory yaml  (``backend/team_templates/*.yaml``):
    team_template_list_factory, team_template_read_factory,
    team_template_write_factory.

  Running team  (DB SSoT per ``team_sessions``):
    team_template_get_running, team_template_patch_role,
    team_template_history, team_template_rollback,
    team_template_reset_role, team_template_yaml_diff,
    team_template_reload.

Trust model: delegates to the live backend via the RuntimeRpcServer Unix
socket. No HTTP, no API key — same pattern as ``tools/mcp_admin_server.py``.

Decision 2026-05-17 (factory yaml = factory default, not continuous source):
* ``write_factory`` mutates the yaml only. It does NOT update running teams.
  To apply the yaml edit to a live team, call ``yaml_diff`` to confirm
  drift, then ``reset_role`` (per role) or ``reload`` (destructive: kills
  agents) once the user has confirmed.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.runtime_rpc_client import RuntimeRpcError, call as rpc_call  # noqa: E402

logger = logging.getLogger("team_template_server")
mcp = FastMCP("TeamTemplate")


def _bridge_error(exc: Exception) -> dict:
    return {
        "error": (
            "Could not reach the main backend RPC bridge. The team_template "
            f"subprocess is up but the runtime socket isn't responding: {exc}"
        ),
        "status": 503,
    }


def _delegate(method: str, params: dict | None = None) -> dict:
    """Pass through to the in-process RPC handler.

    No experimental gate (per user decision 2026-05-31): team-template edit
    is part of the standard self-management surface.
    """
    try:
        return rpc_call(method, params)
    except RuntimeRpcError as exc:
        return _bridge_error(exc)


# ── Factory yaml ──────────────────────────────────────────────────────────


@mcp.tool()
def team_template_list_factory() -> dict:
    """List every team-template yaml in ``backend/team_templates/``.

    Returns ``{"templates": [{name, filename, display_name, description, size, exists}]}``.
    These are the **factory defaults** used at team-creation time. Editing
    them does NOT touch already-running teams — see ``team_template_yaml_diff``
    for the drift view.
    """
    return _delegate("team_template.factory.list")


@mcp.tool()
def team_template_read_factory(name: str) -> dict:
    """Read one factory yaml.

    Args:
        name: filename stem (``agile_team``, ``research_team``, ...).

    Returns ``{name, filename, content, parsed, exists, size}``. When the
    yaml is malformed, ``parsed`` is ``None`` and ``parse_error`` carries
    the parser message — useful to recover a hand-edited file.
    """
    return _delegate("team_template.factory.read", {"name": name})


@mcp.tool()
def team_template_write_factory(name: str, content: str) -> dict:
    """Overwrite one factory yaml (creates if missing).

    Args:
        name: filename stem. New names create a new yaml.
        content: full yaml text. Validated with ``yaml.safe_load`` before
                 write; rejected on parse error.

    Side effects: rotates previous content into ``<name>.yaml.bak``,
    atomic write via temp + rename. Does NOT update running teams. Use
    ``team_template_yaml_diff`` to see drift and ``team_template_reload``
    (destructive) or ``team_template_reset_role`` to apply per running team.
    """
    return _delegate(
        "team_template.factory.write",
        {"name": name, "content": content},
    )


# ── Running team ──────────────────────────────────────────────────────────


@mcp.tool()
def team_template_get_running(session_id: str) -> dict:
    """Return the live template for a team session (DB SSoT, no cache).

    Args:
        session_id: team-session id (e.g. ``agile-team_ccd1adb9``).
    """
    return _delegate("team_template.running.get", {"session_id": session_id})


@mcp.tool()
def team_template_patch_role(
    session_id: str,
    role: str,
    patch: dict[str, Any],
    comment: str = "",
) -> dict:
    """Edit one role's config in a running team. Writes an audit row per field.

    Args:
        session_id: team-session id.
        role: role key (``pm``, ``qe``, ``dev``, ...).
        patch: subset of {instruction, servers, skills, server_overrides, model, role_display}.
        comment: free-text audit comment (shown in history UI).

    Returns ``{audit_ids, diff, after_role, edited_at}``. Edit is
    persisted to DB only — to survive a team recreate, also commit the
    equivalent change to the factory yaml via ``team_template_write_factory``.
    """
    return _delegate(
        "team_template.running.patch_role",
        {"session_id": session_id, "role": role, "patch": patch, "comment": comment},
    )


@mcp.tool()
def team_template_history(
    session_id: str,
    role: str | None = None,
    limit: int = 50,
) -> dict:
    """Audit log for a team session (newest first).

    Args:
        session_id: team-session id.
        role: filter by role (omit for all).
        limit: max rows (1..500, default 50).
    """
    return _delegate(
        "team_template.running.history",
        {"session_id": session_id, "role": role, "limit": limit},
    )


@mcp.tool()
def team_template_rollback(
    session_id: str,
    audit_id: int,
    comment: str = "",
) -> dict:
    """Revert one field to the ``before`` value of the named audit row.

    Args:
        session_id: team-session id.
        audit_id: id from ``team_template_history``.
        comment: free-text reason.

    Writes a NEW audit row (``source='rollback'``); the original row is kept.
    If multiple edits touched the same field after the target, rolling back
    will silently discard the intermediate edits — pick the right audit_id.
    """
    return _delegate(
        "team_template.running.rollback",
        {"session_id": session_id, "audit_id": audit_id, "comment": comment},
    )


@mcp.tool()
def team_template_reset_role(
    session_id: str,
    role: str,
    comment: str = "",
) -> dict:
    """Reset one role's config back to its factory yaml defaults.

    Args:
        session_id: team-session id.
        role: role key.
        comment: free-text audit reason.

    Other roles' state (including UI edits) is preserved. Use this after
    ``team_template_write_factory`` to pull a yaml edit into a running team
    without killing the agents (cf. ``team_template_reload`` which does kill).
    """
    return _delegate(
        "team_template.running.reset_role",
        {"session_id": session_id, "role": role, "comment": comment},
    )


@mcp.tool()
def team_template_yaml_diff(session_id: str) -> dict:
    """Compare the running team's template with its factory yaml.

    Args:
        session_id: team-session id.

    Returns ``{in_sync, diverged_count, per_role: {<role>: {status, fields}}}``.
    READ-ONLY — never mutates the running team. Use to inspect drift after a
    factory-yaml edit before choosing ``reset_role`` or ``reload``.
    """
    return _delegate(
        "team_template.running.yaml_diff",
        {"session_id": session_id},
    )


@mcp.tool()
def team_template_reload(
    session_id: str,
    roles: list[str],
    inject_message: str | None = None,
) -> dict:
    """DESTRUCTIVE — SIGKILL + respawn every agent in the named roles.

    Args:
        session_id: team-session id.
        roles: role keys to reload. Must be non-empty.
        inject_message: optional override for the sentinel message injected
                        into respawned agents' inbox (default: generic notice).

    Use only after user confirmation (the UI shows a warning before calling).
    Agents mid-task are killed without a wait-for-idle. Respawn re-reads the
    DB SSoT, so any patches applied to those roles take effect immediately.
    """
    return _delegate(
        "team_template.running.reload",
        {
            "session_id": session_id,
            "roles": roles,
            "inject_message": inject_message,
        },
    )


if __name__ == "__main__":
    mcp.run()
