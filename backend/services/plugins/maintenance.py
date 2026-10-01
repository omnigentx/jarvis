"""Scheduled expiration of disposable plugin bytes, independent of UI updates."""

from __future__ import annotations

import asyncio
import logging

from services.plugins.lifecycle import PluginLifecycle

logger = logging.getLogger(__name__)
_started: set[str] = set()


def start_maintenance(lifecycle: PluginLifecycle) -> None:
    """A daily housekeeping job; never polls agent or tool execution state."""
    identity = str(lifecycle.root.resolve())
    if identity in _started:
        return
    loop = asyncio.get_running_loop()
    _started.add(identity)

    async def expire() -> None:
        try:
            expired = await asyncio.to_thread(lifecycle.cleanup)
            from sqlalchemy import text

            from services.plugins.resources import cleanup_orphans

            with lifecycle.engine.connect() as db:
                tracked = set(
                    db.execute(text("SELECT id FROM plugin_candidates")).scalars()
                )
            await asyncio.to_thread(cleanup_orphans, lifecycle.root, tracked)
            from services.plugins.ipc import socket_path
            from services.plugins.ipc_maintenance import cleanup_sockets

            await cleanup_sockets(
                socket_path(str(lifecycle.engine.url.database), "maintenance").parent
            )
            from services.activity_stream import activity_stream_manager

            for candidate in expired:
                activity_stream_manager.broadcast(
                    {
                        "event_type": "plugin_status",
                        "agent_name": "",
                        "data": {"id": candidate, "status": "expired"},
                    }
                )
        except Exception:
            logger.warning("[plugins] Scheduled expiration failed", exc_info=True)
        finally:
            if not loop.is_closed():
                loop.call_later(86400, lambda: loop.create_task(expire()))

    loop.create_task(expire())
