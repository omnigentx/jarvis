"""Dynamic agent loader & reload service (DB-backed).

Replaces the legacy file-based agent_card loader. Definitions now live
in SQLite (table `agent_definitions`, see ``services.agent_definitions``).
The parent process polls a monotonic ``rev`` counter on that store and
calls ``agent_app.load_agent_data(defs, "Jarvis")`` whenever rev advances,
so subprocess writers (the agent_spawner MCP, REST CRUD endpoints) only
need to bump rev — no signal files, no filesystem watching.

Why poll the DB instead of pushing? Mutations may originate inside
subprocesses (the MCP server runs separately) that don't share Python
state with the parent FastAgent. SQLite is the only state shared across
processes, so polling a meta column is the simplest correct mechanism.
The poll interval is small (2s) — well under interactive expectations
and cheap (one indexed read).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2.0  # seconds

# Parent name agents created at runtime get attached to. Jarvis is the
# master agent; dynamic sub-agents become tools of Jarvis. If/when this
# project evolves to support multiple masters, this string moves to
# config — for now hard-coding is simpler and avoids speculative API.
_PARENT_AGENT = "Jarvis"


def _definitions_for_load() -> list[dict[str, Any]]:
    """Read every dynamic agent definition from DB and shape it for
    ``load_agent_data``. Returns an empty list if the DB isn't configured
    (test harness without SPAWN_REGISTRY_DB and no data/jarvis.db)."""
    from services import agent_definitions as svc

    rows = svc.list_definitions()
    out: list[dict[str, Any]] = []
    for row in rows:
        card: dict[str, Any] = {
            "name": row["name"],
            "instruction": row["instruction"],
            "servers": row["servers"],
            "tools": row["tools"],
            "skills": row["skills"],
            "use_history": row["use_history"],
        }
        if row.get("model"):
            card["model"] = row["model"]
        if row.get("request_params"):
            card["request_params"] = row["request_params"]
        out.append(card)
    return out


async def preload_dynamic_agents(agent_app) -> list[str]:
    """Load every dynamic agent definition from DB at server startup.

    Returns the sorted list of agent names loaded. Empty list when the
    DB is empty or unavailable. Never raises — startup must not fail
    because a single bad definition is on disk.
    """
    defs = _definitions_for_load()
    if not defs:
        logger.info("[DYNAMIC] No dynamic agent definitions found in DB")
        return []

    try:
        loaded, _attached = await agent_app.load_agent_data(defs, _PARENT_AGENT)
        logger.info("[DYNAMIC] ✓ Pre-loaded dynamic agents from DB: %s", loaded)
        return loaded
    except Exception as e:  # noqa: BLE001
        logger.error(
            "[DYNAMIC] Failed to pre-load dynamic agents from DB: %s",
            e,
            exc_info=True,
        )
        return []


async def db_rev_poll_loop(agent_app) -> None:
    """Background task: poll agent_definitions_meta.rev; reload on change.

    The rev counter is bumped by:
    - The REST CRUD endpoints (services.agent_definitions.create/update/
      delete) when an operator edits an agent via the dashboard.
    - The `spawn_agent` MCP tool when Jarvis self-creates an agent.

    Both writer paths run in different processes (subprocess MCP) or in
    request handlers that don't hold the FastAgent state, so this loop
    is the converge mechanism. One-shot polling per tick: read rev,
    compare, reload if changed.
    """
    from services import agent_definitions as svc

    last_rev = svc.get_rev()
    logger.info(
        "[DYNAMIC] DB-rev poll loop started (interval %.1fs, initial rev=%d)",
        POLL_INTERVAL,
        last_rev,
    )

    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL)

            current = svc.get_rev()
            if current == last_rev:
                continue

            logger.info(
                "[DYNAMIC] rev %d → %d, reloading from DB", last_rev, current
            )

            try:
                defs = _definitions_for_load()
                loaded, _attached = await agent_app.load_agent_data(
                    defs, _PARENT_AGENT
                )
                logger.info("[DYNAMIC] ✓ Reloaded from DB: %s", loaded)

                # Newly registered dynamic agents need the always-on
                # token-persistence hook attached too; otherwise their
                # LLM calls would bypass token_usage tracking. The
                # helper is idempotent per-agent (sentinel attribute),
                # so re-running on each reload is safe.
                try:
                    from services.sse_progress import (
                        attach_token_persistence_hooks_to_all,
                    )

                    attach_token_persistence_hooks_to_all(agent_app)
                except Exception as _e:  # noqa: BLE001
                    logger.warning(
                        "[DYNAMIC] Failed to re-attach token hook after reload: %s",
                        _e,
                    )

                last_rev = current
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "[DYNAMIC] Reload failed at rev %d: %s",
                    current,
                    e,
                    exc_info=True,
                )
                # Don't advance last_rev — retry on next tick.

        except asyncio.CancelledError:
            logger.info("[DYNAMIC] DB-rev poll loop stopped")
            break
        except Exception as e:  # noqa: BLE001
            logger.error("[DYNAMIC] Unexpected error in poll loop: %s", e)
            await asyncio.sleep(5)  # Back off on errors
