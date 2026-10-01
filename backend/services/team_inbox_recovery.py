"""One startup reconciliation of durable team inboxes; no polling or new messages."""

from __future__ import annotations
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def recover_team_inboxes(registry: Any) -> None:
    """Wake the latest scoped run for unread messages, preserving pause intent.

    Existing framework socket liveness and launch claims serialize recovery with
    manual injects and other wake sources. The inbox remains the source of truth:
    this step never acknowledges, copies or invents messages.
    """
    from fast_agent.spawn.message_bus import MessageBus
    from fast_agent.spawn.servers._team_helpers import wake_team_agent

    latest: dict[tuple[str, str], dict] = {}
    for record in registry.get_all().values():
        session = record.get("session_id") or (record.get("original_config") or {}).get(
            "env_vars", {}
        ).get("TEAM_SESSION_ID")
        name = record.get("agent_name")
        if not session or not name:
            continue
        key = (session, name)
        if key not in latest or (record.get("started_at") or 0) > (
            latest[key].get("started_at") or 0
        ):
            latest[key] = record
    for (session, name), record in latest.items():
        if record.get("status") in {"paused", "available"}:
            continue
        env = (record.get("original_config") or {}).get("env_vars") or {}
        directory = env.get("TEAM_MESSAGES_DIR")
        if not directory or not Path(directory).is_dir() or not record.get("run_id"):
            continue
        try:
            if MessageBus(directory).read_unread(name):
                outcome = wake_team_agent(session, name, record["run_id"])
                logger.info("[INBOX-RECOVERY] %s/%s: %s", session, name, outcome)
        except Exception:
            logger.exception(
                "[INBOX-RECOVERY] Pending messages retained for %s/%s", session, name
            )
