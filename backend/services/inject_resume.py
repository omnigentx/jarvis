"""Resume non-running agent with context from DB for prompt injection.

Agents are nodes in the team tree. Inject = send context/direction to a node
WITHOUT disrupting the team orchestration flow. After processing the inject,
the agent continues under Jarvis's coordination via _check_and_resume_on_inbox.

Flow:
  1. Load latest context snapshot from DB (single source of truth)
  2. Write context → temp history .json file (for subprocess restore)
  3. Re-inject team context (roster via TEAM_SESSION_ID) if team agent
  4. Spawn via run_isolated_agent_background with same env_vars, servers, etc.
  5. Events flow in-process: child stderr → DisplayManager → bridge.process_event()
  6. After completion → _check_and_resume_on_inbox → agent continues team flow

Zero changes to fast-agent submodule — uses existing history_file mechanism.
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def resume_with_inject(
    agent_name: str,
    inject_message: str,
    spawn_record: dict,
    bridge: Any | None = None,
) -> dict:
    """Resume a non-running agent with inject message, preserving team flow.

    The agent wakes up with full conversation history from DB,
    processes the inject message, then checks inbox for team messages
    and continues orchestration flow via _check_and_resume_on_inbox.

    Args:
        agent_name: Unique agent identifier (e.g. "Khoi [SA]")
        inject_message: User's directive to inject
        spawn_record: Latest spawn record from AgentRegistryDB
        bridge: SpawnProgressBridge for in-process event forwarding

    Returns:
        {"status": "resumed", "run_id": new_run_id, "agent_name": agent_name}

    Raises:
        ValueError: If original_config is missing
    """
    original_config = spawn_record.get("original_config", {})
    if not original_config:
        raise ValueError(
            f"No original_config for agent '{agent_name}'. Cannot resume."
        )

    # 0. Refresh role-level config from the live team template.
    #
    # Why: ``original_config`` is a FROZEN snapshot taken at first spawn.
    # When a user edits the team template later (add an MCP server, change
    # instruction, swap a skill), the canonical state lives in
    # ``team_sessions.template.roles[role]`` — but unless we re-read it here,
    # resume passes the stale 7-server list and the new tool silently
    # disappears (incident 2026-05-18: ``playwright`` added to QE template
    # via patch script, but every resume still got the old server list
    # because we were reading the snapshot, not the canonical row).
    #
    # The /reload endpoint already kills + re-resumes the agent — but the
    # respawn only picks up template changes if THIS function reads them.
    # See ``services/team_reload.py`` for the kill half of the flow.
    team_session_id = (original_config.get("env_vars") or {}).get(
        "TEAM_SESSION_ID", ""
    )
    role = original_config.get("role", "")
    fresh_role_cfg = _load_role_from_template(team_session_id, role)
    if fresh_role_cfg is not None:
        _log_template_drift(agent_name, original_config, fresh_role_cfg)

    # 1. Load context from DB — single source of truth
    context_json = _load_context_from_db(agent_name)
    history_file = None

    if context_json:
        # Write to temp file for subprocess restore via load_history_into_agent
        history_file = _write_temp_history(agent_name, context_json, original_config)
        logger.info(
            "[INJECT-RESUME] Loaded context from DB for %s → %s",
            agent_name, history_file,
        )
    else:
        logger.warning(
            "[INJECT-RESUME] No context snapshot for %s — agent starts fresh",
            agent_name,
        )

    # 2. Prepare team context and env_vars
    env_vars = original_config.get("env_vars") or {}
    project_dir = original_config.get(
        "project_dir",
        os.environ.get("SPAWN_PROJECT_DIR", "."),
    )

    # Re-inject team roster if this is a team agent
    enriched_context = inject_message
    if team_session_id:
        enriched_context = _reinject_team_context(
            inject_message, team_session_id, role,
        )

    # Resolve EACH spawn input through one helper so the SSoT rule
    # ("prefer live template; fall back to snapshot") is in one place
    # rather than scattered across keyword arguments.
    def _resolve(key: str, default: Any = None) -> Any:
        if fresh_role_cfg is not None and key in fresh_role_cfg:
            value = fresh_role_cfg.get(key)
            if value is not None:
                return value
        return original_config.get(key, default)

    servers = _resolve("servers", [])
    server_overrides = _resolve("server_overrides", None)
    instruction = _resolve("instruction", "")
    skills = _resolve("skills", [])
    model = _resolve("model", "")

    # 3. Create SpawnRegistry (same file as MCP spawner — concurrent-safe)
    registry = _create_registry(project_dir)

    # 4. Create DisplayManager that forwards events to SpawnProgressBridge
    display_manager = _create_bridge_display(bridge) if bridge else None

    # 5. Spawn agent via existing mechanism
    from fast_agent.spawn.isolated_spawner import run_isolated_agent_background

    new_run_id = await run_isolated_agent_background(
        task=inject_message,
        project_dir=project_dir,
        instruction=instruction,
        context=enriched_context,
        servers=servers,
        model=model,
        timeout_seconds=original_config.get("timeout_seconds", 0),
        role=role,
        agent_name=agent_name,
        team_name=original_config.get("team_name", spawn_record.get("team_name", "")),
        workspace_dir=original_config.get("workspace_dir") or None,
        lifecycle="resumable",
        registry=registry,
        display_manager=display_manager,
        env_vars=env_vars or None,
        skills=skills,
        history_file=history_file,
        spawn_lifecycle_hooks=None,  # Subprocess events (stderr) are sufficient
        session_id=team_session_id,
        # Without this, every dashboard "Send message" / inject path falls back
        # to the BASE fastagent.config.yaml filesystem args (./data only) and
        # the agent silently loses workspace + skills access (incident
        # 2026-05-17: Designer broken filesystem after restore — fix landed in
        # auto-resume/restart/resume but THIS code path was missed).
        server_overrides=server_overrides,
    )

    logger.info(
        "[INJECT-RESUME] Agent %s resumed as run_id=%s (team=%s, has_context=%s)",
        agent_name, new_run_id,
        original_config.get("team_name", "none"),
        context_json is not None,
    )

    return {
        "status": "resumed",
        "run_id": new_run_id,
        "agent_name": agent_name,
    }


# ── Helper functions ─────────────────────────────────────────────────────


def _load_role_from_template(
    team_session_id: str,
    role: str,
) -> dict | None:
    """Read the live role config from ``team_sessions.template.roles[role]``.

    Returns the role dict (with keys like ``servers``, ``server_overrides``,
    ``instruction``, ``skills``, ``model``) or ``None`` when:

    - ``team_session_id`` is empty (this is an ad-hoc spawn, not team-managed)
    - the role is empty
    - the team session no longer exists (deleted / migrated)
    - the role was removed from the template after the agent spawned

    Callers MUST treat ``None`` as "fall back to original_config snapshot"
    — never as an error. The point of this helper is purely to refresh
    template-driven fields while keeping the registry snapshot as a safety
    net for non-template fields (env_vars, workspace_dir, project_dir).
    """
    if not team_session_id or not role:
        return None
    try:
        from fast_agent.spawn.team_spawner import get_team_session

        session = get_team_session(team_session_id)
    except Exception as exc:
        logger.warning(
            "[INJECT-RESUME] Failed to load team session '%s' for fresh "
            "role refresh: %s — falling back to original_config.",
            team_session_id, exc,
        )
        return None
    if session is None:
        return None
    roles = (session.template or {}).get("roles") or {}
    role_cfg = roles.get(role)
    if not isinstance(role_cfg, dict):
        return None
    return role_cfg


def _log_template_drift(
    agent_name: str,
    original_config: dict,
    fresh_role_cfg: dict,
) -> None:
    """Log when template-driven fields differ from the snapshot.

    Silent drift is exactly how the 2026-05-18 ``playwright`` incident hid
    — patch updated the canonical template but every resume read the stale
    snapshot. By emitting a single INFO line listing the diff, the next
    operator who tails the log can immediately see "ah, the live template
    is ahead of the snapshot, that's why this agent has different servers
    after resume" instead of spelunking two tables.

    Best-effort only — no exception raised. We never want this helper to
    block a resume.
    """
    try:
        changes: list[str] = []
        for key in ("servers", "server_overrides", "instruction", "skills", "model"):
            old = original_config.get(key)
            new = fresh_role_cfg.get(key, old)
            if old != new:
                if key in ("servers", "skills"):
                    old_set = set(old or [])
                    new_set = set(new or [])
                    added = sorted(new_set - old_set)
                    removed = sorted(old_set - new_set)
                    parts = []
                    if added:
                        parts.append(f"+{added}")
                    if removed:
                        parts.append(f"-{removed}")
                    if parts:
                        changes.append(f"{key} {' '.join(parts)}")
                else:
                    changes.append(f"{key} changed")
        if changes:
            logger.info(
                "[INJECT-RESUME] Template drift for %s — using live template: %s",
                agent_name, "; ".join(changes),
            )
    except Exception as exc:  # pragma: no cover — diagnostic only
        logger.debug("[INJECT-RESUME] drift logging failed: %s", exc)


def _load_context_from_db(agent_name: str) -> str | None:
    """Load the NEWEST context as raw JSON — across both raw snapshots
    and compaction working snapshots (newest-wins, see
    load_latest_context_json_any for why "prefer working" would lose
    turns recorded after a compaction)."""
    try:
        from services.context_persistence import load_latest_context_json_any
        return load_latest_context_json_any(agent_name)
    except Exception as exc:
        logger.error(
            "[INJECT-RESUME] Failed to load context for %s: %s",
            agent_name, exc, exc_info=True,
        )
        return None


def _write_temp_history(
    agent_name: str,
    context_json: str,
    original_config: dict,
) -> str:
    """Write context JSON to a temp file for subprocess history restore.

    File is placed in workspace dir (if available) or system temp.
    Uses .json extension so load_history_into_agent recognizes it.
    """
    workspace_dir = original_config.get("workspace_dir", "")
    if workspace_dir and os.path.isdir(workspace_dir):
        target_dir = workspace_dir
    else:
        target_dir = tempfile.gettempdir()

    # Ensure target dir exists
    os.makedirs(target_dir, exist_ok=True)

    safe_name = agent_name.replace(" ", "_").replace("/", "_")
    file_path = os.path.join(target_dir, f".inject_context_{safe_name}.json")

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(context_json)

    return file_path


def _reinject_team_context(
    inject_message: str,
    team_session_id: str,
    role: str,
) -> str:
    """Re-inject team roster context for team agents.

    Same pattern as resume_spawn in agent_spawner_server.py (lines 650-665).
    """
    try:
        from fast_agent.spawn.team_spawner import get_team_session

        session = get_team_session(team_session_id)
        if session:
            roster_ctx = session.roster_context(for_role=role)
            logger.info(
                "[INJECT-RESUME] Re-injected team context (session %s)",
                team_session_id,
            )
            return roster_ctx + "\n\n" + inject_message
    except Exception as exc:
        logger.warning(
            "[INJECT-RESUME] Failed to re-inject team context: %s", exc,
        )

    return inject_message


def _create_registry(project_dir: str) -> Any:
    """Create SpawnRegistry instance pointing to the same file as MCP spawner.

    SpawnRegistry uses file locks for concurrent access — safe to create
    multiple instances from different processes pointing to the same file.
    """
    from fast_agent.spawn.spawn_registry import SpawnRegistry

    registry_path = (
        Path(project_dir).resolve()
        / ".runtime" / "state" / "spawn_registry.json"
    )
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    return SpawnRegistry(registry_file=str(registry_path))


def _create_bridge_display(bridge: Any) -> Any:
    """Create DisplayManager that forwards events directly to SpawnProgressBridge.

    When inject is called from the main backend process (not MCP subprocess),
    events from the child subprocess need to reach SpawnProgressBridge.
    Instead of going through the Unix socket, we forward in-process.
    """
    from fast_agent.spawn.spawn_display import SpawnDisplayManager

    dm = SpawnDisplayManager()

    def _forward_to_bridge(event: Any) -> None:
        """Convert SpawnEvent → JSON line → bridge.process_event()."""
        try:
            line = json.dumps({
                "timestamp": time.time(),
                "agent_name": (
                    getattr(event, "agent_name", "")
                    or getattr(event, "role", "")
                ),
                "event_type": getattr(event, "event", ""),
                "run_id": getattr(event, "run_id", ""),
                "data": getattr(event, "data", {}),
            })
            bridge.process_event(line)
        except Exception as exc:
            logger.warning("[INJECT-RESUME] Bridge forward failed: %s", exc)

    dm.set_event_callback(_forward_to_bridge)
    return dm
