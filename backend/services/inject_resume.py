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
    team_session_id = env_vars.get("TEAM_SESSION_ID", "")
    if team_session_id:
        enriched_context = _reinject_team_context(
            inject_message, team_session_id, original_config.get("role", ""),
        )

    # 3. Create SpawnRegistry (same file as MCP spawner — concurrent-safe)
    registry = _create_registry(project_dir)

    # 4. Create DisplayManager that forwards events to SpawnProgressBridge
    display_manager = _create_bridge_display(bridge) if bridge else None

    # 5. Spawn agent via existing mechanism
    from fast_agent.spawn.isolated_spawner import run_isolated_agent_background

    new_run_id = await run_isolated_agent_background(
        task=inject_message,
        project_dir=project_dir,
        instruction=original_config.get("instruction", ""),
        context=enriched_context,
        servers=original_config.get("servers", []),
        model=original_config.get("model", ""),
        timeout_seconds=original_config.get("timeout_seconds", 0),
        role=original_config.get("role", ""),
        agent_name=agent_name,
        team_name=original_config.get("team_name", spawn_record.get("team_name", "")),
        workspace_dir=original_config.get("workspace_dir") or None,
        lifecycle="resumable",
        registry=registry,
        display_manager=display_manager,
        env_vars=env_vars or None,
        skills=original_config.get("skills", []),
        history_file=history_file,
        spawn_lifecycle_hooks=None,  # Subprocess events (stderr) are sufficient
        session_id=team_session_id,
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


def _load_context_from_db(agent_name: str) -> str | None:
    """Load latest context snapshot as raw JSON string from DB."""
    try:
        from services.context_persistence import load_latest_context_json
        return load_latest_context_json(agent_name)
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
