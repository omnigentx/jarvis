"""Bridge between fast-agent meeting subprocess and the Jarvis SSE event system.

The meeting_room_server runs as an MCP subprocess and writes lifecycle
events to the ``meeting_events`` table in ``jarvis.db``.  This bridge
polls that table (at sub-second intervals) and forwards new events to
the in-process ``MeetingEventManager``, which pushes them to connected
SSE clients.

Usage (in server.py lifespan)::

    from services.meeting_hooks_bridge import MeetingEventBridge
    from services.meeting_events import meeting_event_manager

    bridge = MeetingEventBridge("data/jarvis.db", meeting_event_manager)
    bridge_task = asyncio.create_task(bridge.watch())
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Any

from services.meeting_events import MeetingEventManager, meeting_event_manager

logger = logging.getLogger(__name__)


class MeetingEventBridge:
    """Polls the ``meeting_events`` SQLite table for cross-process events.

    Replaces the legacy JSONL file watcher with direct SQLite reads.
    WAL mode ensures the subprocess can write concurrently while we read.
    """

    def __init__(
        self,
        db_path: str,
        manager: MeetingEventManager | None = None,
    ) -> None:
        self._db_path = db_path
        self._manager = manager or meeting_event_manager
        self._last_event_id = 0

    def _get_max_event_id(self) -> int:
        """Get the current max event ID to skip historical events on startup."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.execute("PRAGMA busy_timeout=3000")
            row = conn.execute(
                "SELECT MAX(id) FROM meeting_events"
            ).fetchone()
            conn.close()
            return row[0] or 0
        except Exception:
            return 0

    def reset_cursor(self) -> None:
        """Skip all existing events — only process new ones from now."""
        self._last_event_id = self._get_max_event_id()
        logger.info(
            "[MeetingBridge] Cursor reset to event_id=%d", self._last_event_id
        )

    async def watch(self, poll_interval: float = 0.5) -> None:
        """Long-lived task: poll meeting_events and relay to MeetingEventManager."""
        logger.info(
            "[MeetingBridge] Watching meeting_events in %s (after_id=%d)",
            self._db_path,
            self._last_event_id,
        )

        while True:
            try:
                await asyncio.sleep(poll_interval)
                new_events = self._poll()

                for event in new_events:
                    event_type = event.get("event_type", "")
                    meeting_id = event.get("meeting_id", "")
                    data = event.get("data", {})

                    if event_type and meeting_id:
                        self._manager.emit(event_type, meeting_id, data)

                    # Advance cursor
                    self._last_event_id = max(
                        self._last_event_id, event.get("id", 0)
                    )

            except asyncio.CancelledError:
                logger.info("[MeetingBridge] Event watcher stopped.")
                raise
            except Exception as e:
                logger.warning("[MeetingBridge] Error polling events: %s", e)
                await asyncio.sleep(2)

    def _poll(self) -> list[dict]:
        """Read new events since last cursor position."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.execute("PRAGMA busy_timeout=3000")
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, event_type, meeting_id, data_json, created_at "
                "FROM meeting_events WHERE id > ? ORDER BY id LIMIT 100",
                (self._last_event_id,),
            ).fetchall()
            conn.close()

            return [
                {
                    "id": r["id"],
                    "event_type": r["event_type"],
                    "meeting_id": r["meeting_id"],
                    "data": json.loads(r["data_json"]) if r["data_json"] else {},
                    "ts": r["created_at"],
                }
                for r in rows
            ]
        except Exception as e:
            logger.debug("[MeetingBridge] Poll error: %s", e)
            return []
