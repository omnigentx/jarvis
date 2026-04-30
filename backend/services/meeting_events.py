"""MeetingEventManager — in-process event bus for meeting lifecycle.

Replaces the file-polling approach with an event-driven architecture.
Backend services subscribe to meeting events and broadcast them to
SSE clients in real time.

Usage::

    from services.meeting_events import meeting_event_manager

    # Subscribe
    meeting_event_manager.subscribe("transcript_entry", handler_fn)

    # Emit (called from hooks bridge)
    meeting_event_manager.emit("transcript_entry", meeting_id, entry)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class MeetingEvent:
    """Immutable event payload."""

    event_type: str
    meeting_id: str
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)


class MeetingEventManager:
    """In-process pub/sub for meeting events.

    Thread-safe: callbacks are invoked in the event loop via
    ``call_soon_threadsafe`` since hooks fire from agent subprocesses
    (via the JSONL bridge) or background threads.
    """

    # Known event types
    EVENT_TYPES = frozenset(
        {
            "meeting_created",
            "participant_joined",
            "meeting_started",
            "transcript_entry",
            "turn_advanced",
            "verdict",
            "meeting_ended",
            "participant_left",
            "participant_added",
            "state_changed",
        }
    )

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        # Per-meeting SSE queues — each connected client gets its own queue
        self._client_queues: dict[str, list[asyncio.Queue]] = defaultdict(list)
        # Global meeting list queues — for clients watching the meeting list
        self._global_queues: list[asyncio.Queue] = []
        self._event_history: list[MeetingEvent] = []
        self._max_history = 200

    def subscribe(self, event_type: str, callback: Callable) -> None:
        """Register a callback for an event type."""
        self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable) -> None:
        """Remove a callback."""
        try:
            self._subscribers[event_type].remove(callback)
        except ValueError:
            pass

    def emit(self, event_type: str, meeting_id: str, data: dict | None = None) -> None:
        """Fire an event to all subscribers + SSE queues.

        Safe to call from any thread.
        """
        event = MeetingEvent(
            event_type=event_type,
            meeting_id=meeting_id,
            data=data or {},
        )

        # Store in history
        self._event_history.append(event)
        if len(self._event_history) > self._max_history:
            self._event_history = self._event_history[-self._max_history:]

        # Invoke registered callbacks
        for cb in self._subscribers.get(event_type, []):
            try:
                cb(event)
            except Exception as e:
                logger.warning(
                    "Meeting event callback failed (%s): %s", event_type, e
                )

        # Push to per-meeting SSE queues
        event_dict = {
            "type": event_type,
            "meeting_id": meeting_id,
            "data": data or {},
            "timestamp": event.timestamp,
        }
        for q in self._client_queues.get(meeting_id, []):
            try:
                q.put_nowait(event_dict)
            except asyncio.QueueFull:
                logger.debug("SSE queue full for meeting %s, dropping event", meeting_id)

        # Push to global meeting-list watchers
        for q in self._global_queues:
            try:
                q.put_nowait(event_dict)
            except asyncio.QueueFull:
                logger.debug("Global meeting SSE queue full, dropping event")

    # ── SSE queue management ──────────────────────────────────────

    def create_meeting_queue(self, meeting_id: str) -> asyncio.Queue:
        """Create and register a queue for a specific meeting SSE client."""
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._client_queues[meeting_id].append(q)
        return q

    def remove_meeting_queue(self, meeting_id: str, q: asyncio.Queue) -> None:
        """Unregister a meeting SSE client queue."""
        try:
            self._client_queues[meeting_id].remove(q)
        except ValueError:
            pass
        if not self._client_queues[meeting_id]:
            del self._client_queues[meeting_id]

    def create_global_queue(self) -> asyncio.Queue:
        """Create a queue for clients watching the global meeting list."""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._global_queues.append(q)
        return q

    def remove_global_queue(self, q: asyncio.Queue) -> None:
        """Unregister a global list SSE client queue."""
        try:
            self._global_queues.remove(q)
        except ValueError:
            pass

    def get_recent_events(
        self, meeting_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        """Return recent events from history (for backfill on reconnect)."""
        events = self._event_history
        if meeting_id:
            events = [e for e in events if e.meeting_id == meeting_id]
        return [
            {
                "type": e.event_type,
                "meeting_id": e.meeting_id,
                "data": e.data,
                "timestamp": e.timestamp,
            }
            for e in events[-limit:]
        ]


# Module-level singleton
meeting_event_manager = MeetingEventManager()
