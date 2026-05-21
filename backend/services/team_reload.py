"""Force-kill + respawn agents in a team after a template change.

The destructive half of the template edit flow. ``team_template_service``
mutates DB SSoT; this module brings live processes into alignment.

Semantics (per user decision 2026-05-17):
  - No "wait for idle". Kill immediately on user confirmation.
  - Respawn via the existing inject_resume path so context + history are
    preserved through the restart.
  - Per-role scope: reload only the roles the user named.

Re-uses the same SSoT chain everyone else does:
  1. spawn_record (latest by agent_name) → kill its PID tree
  2. resume_with_inject() → reads fresh team_sessions.template from DB
     (no caching) → spawns with new config

The inject message is a sentinel agents are told to acknowledge silently;
they don't have to react beyond reading their inbox.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel sent to the agent when reload is triggered. Kept short so it
# doesn't clutter the context window if the agent decides to log it.
_RELOAD_SENTINEL = (
    "[SYSTEM] Your team template was updated by an administrator and your "
    "process was restarted. Tools / skills / instructions may have changed. "
    "Continue with your previous task; if uncertain, re-read your skills."
)

# Inject statuses (mirrors routes/inject.py). Keep in sync if those change.
_PROCESS_ALIVE_STATUSES = {"running", "pending", "paused"}


def _kill_process_tree(pid: int, *, sigkill_after: float = 2.0) -> bool:
    """SIGTERM then SIGKILL after grace. Returns True if PID is gone."""
    if pid <= 0:
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True  # already dead
    except PermissionError:
        logger.warning("[RELOAD] kill SIGTERM permission denied for pid=%d", pid)
        return False

    # Wait briefly for graceful exit
    deadline = time.time() + sigkill_after
    while time.time() < deadline:
        try:
            os.kill(pid, 0)  # signal 0 = liveness probe
        except ProcessLookupError:
            return True
        time.sleep(0.1)

    # Still alive — escalate
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        logger.warning("[RELOAD] kill SIGKILL permission denied for pid=%d", pid)
        return False

    # Final liveness check
    time.sleep(0.2)
    try:
        os.kill(pid, 0)
        logger.warning("[RELOAD] pid=%d still alive after SIGKILL", pid)
        return False
    except ProcessLookupError:
        return True


def _list_agents_for_role(session: Any, role: str) -> list[tuple[str, dict[str, Any]]]:
    """Return ``[(agent_name, agent_info_dict), ...]`` for agents in this session
    matching the role. ``agent_info_dict`` is the value of
    ``session.agents[name]`` (has run_id, role, status, ...).
    """
    out: list[tuple[str, dict[str, Any]]] = []
    for name, info in (session.agents or {}).items():
        if (info or {}).get("role") == role:
            out.append((name, info))
    return out


async def reload_roles(
    session_id: str,
    roles: list[str],
    *,
    edited_by: str = "system",
    inject_message: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Kill + respawn every agent in ``roles`` for the given session.

    Returns ``{role: [{agent_name, old_run_id, old_pid, new_run_id, killed,
    resumed}]}``. Per-agent errors are caught and reported in-band so a
    single failure doesn't break the whole reload.

    The respawn flow goes through ``services.inject_resume.resume_with_inject``.
    That function now re-reads ``team_sessions.template.roles[role]`` at
    resume time and overrides the spawn registry's frozen snapshot for
    template-driven fields (servers / server_overrides / instruction /
    skills / model). Before 2026-05-18 it silently read the snapshot, so
    a template edit landed in DB but never reached the next resume —
    incident: `playwright` added to QE servers list, every resume kept
    using the stale 7-server snapshot, agent fell back to scrapling-server.
    """
    from fast_agent.spawn.team_spawner import get_team_session
    from services import shared_state
    from services.inject_resume import resume_with_inject

    session = get_team_session(session_id)
    if session is None:
        raise LookupError(f"team session '{session_id}' not found")

    msg = inject_message or _RELOAD_SENTINEL
    results: dict[str, list[dict[str, Any]]] = {}

    for role in roles:
        results[role] = []
        targets = _list_agents_for_role(session, role)
        if not targets:
            logger.warning(
                "[RELOAD] role=%s in session=%s has no agents — skipping",
                role, session_id,
            )
            continue

        for agent_name, info in targets:
            entry: dict[str, Any] = {
                "agent_name": agent_name,
                "old_run_id": info.get("run_id"),
                "old_pid": None,
                "new_run_id": None,
                "killed": False,
                "resumed": False,
                "error": None,
            }
            try:
                # 1. Find latest spawn record + its PID
                if not shared_state.registry_db:
                    raise RuntimeError("registry_db not initialised")
                records = shared_state.registry_db.find_by_name(agent_name)
                latest = records[0] if records else None
                if not latest:
                    raise LookupError(
                        f"no spawn record for agent '{agent_name}'"
                    )

                pid = latest.get("pid")
                entry["old_pid"] = pid

                # 2. Kill if alive
                if latest.get("status") in _PROCESS_ALIVE_STATUSES and pid:
                    entry["killed"] = _kill_process_tree(int(pid))
                    if not entry["killed"]:
                        # Continue anyway — respawn will create a fresh run
                        # under a new pid; the old will eventually exit or be
                        # cleaned by the reaper.
                        logger.warning(
                            "[RELOAD] could not confirm kill of pid=%s for %s",
                            pid, agent_name,
                        )

                # 3. Respawn via inject_resume — picks up fresh DB template
                resume_result = await resume_with_inject(
                    agent_name=agent_name,
                    inject_message=msg,
                    spawn_record=latest,
                    bridge=None,
                )
                entry["new_run_id"] = resume_result.get("run_id")
                entry["resumed"] = resume_result.get("status") == "resumed"

            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "[RELOAD] failed for session=%s role=%s agent=%s: %s",
                    session_id, role, agent_name, exc, exc_info=True,
                )

            results[role].append(entry)

            # Brief gap between agents so spawn events don't pile up — also
            # gives the spawned subprocess a head-start to claim the run_id.
            await asyncio.sleep(0.1)

        logger.info(
            "[RELOAD] session=%s role=%s reloaded %d agent(s) by %s",
            session_id, role, len(results[role]), edited_by,
        )

    return results


def find_sessions_using_skill(skill_name: str) -> list[dict[str, Any]]:
    """Scan every team_sessions row for roles that reference ``skill_name``.

    Returns ``[{session_id, team_name, roles: [role_keys...]}]`` — one entry
    per affected session, only roles whose ``skills`` list contains the skill.
    Used by the skill-reload endpoint to compute blast radius before showing
    the confirm dialog.
    """
    from fast_agent.spawn.team_spawner import list_team_sessions

    matches: list[dict[str, Any]] = []
    for sess in list_team_sessions():
        roles = (sess.get("template") or {}).get("roles") or {}
        affected_roles = [
            rk for rk, rcfg in roles.items()
            if skill_name in ((rcfg or {}).get("skills") or [])
        ]
        if affected_roles:
            matches.append({
                "session_id": sess.get("session_id"),
                "team_name": sess.get("team_name", ""),
                "roles": affected_roles,
            })
    return matches


async def reload_by_skill(
    skill_name: str,
    *,
    edited_by: str = "system",
    inject_message: str | None = None,
) -> dict[str, Any]:
    """Force-kill + respawn every agent across every team that uses ``skill_name``.

    Returns ``{skill: name, sessions: [{session_id, results}]}`` where
    ``results`` is exactly the per-role dict returned by ``reload_roles``.
    Cross-team operation — a single skill is typically used by many roles in
    many teams (e.g. ``team-communication``, ``self-audit-tools``).
    """
    sentinel = inject_message or (
        f"[SYSTEM] The skill '{skill_name}' was updated by an administrator "
        f"and your process was restarted. Re-read the skill before continuing "
        f"your previous task."
    )
    affected = find_sessions_using_skill(skill_name)
    out: dict[str, Any] = {"skill": skill_name, "sessions": []}
    for entry in affected:
        sid = entry["session_id"]
        try:
            results = await reload_roles(
                session_id=sid,
                roles=entry["roles"],
                edited_by=edited_by,
                inject_message=sentinel,
            )
        except Exception as exc:
            logger.error(
                "[RELOAD/skill=%s] session=%s failed: %s",
                skill_name, sid, exc, exc_info=True,
            )
            results = {"_error": [{"error": f"{type(exc).__name__}: {exc}"}]}
        out["sessions"].append({
            "session_id": sid,
            "team_name": entry["team_name"],
            "roles": entry["roles"],
            "results": results,
        })
    return out
