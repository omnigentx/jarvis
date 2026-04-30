"""
Pre-gen Stream Manager — SSE broadcast for TTS pre-generation events.

Provides real-time updates when chapters are being generated, complete,
or when the priority queue changes.

Pattern: Same as ActivityStreamManager — per-subscriber asyncio.Queue fanout.

Event types:
  - chapter_generating: {story_id, chapter_file}
  - chapter_ready:      {story_id, chapter_file, duration_s, size_mb}
  - chapter_error:      {story_id, chapter_file, error}
  - queue_update:       {queue: [{story_id, chapter_file, priority}, ...]}
  - scheduler_idle:     {}
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class PregenStreamManager:
    """Manages SSE subscribers for TTS pre-generation events.

    Each subscriber (SSE client) gets its own asyncio.Queue.
    broadcast() fans out events to all active subscribers.
    Supports optional story_id filter per subscriber.
    """

    def __init__(self):
        self._subscribers: dict[str, asyncio.Queue] = {}
        self._counter = 0

    def subscribe(self, story_id: Optional[str] = None) -> tuple[str, asyncio.Queue]:
        """Create a new subscriber. Returns (subscriber_id, queue).

        Args:
            story_id: If set, only events for this story are delivered.
        """
        self._counter += 1
        sub_id = f"pregen_{self._counter}_{int(time.time())}"
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        q._story_filter = story_id  # type: ignore[attr-defined]
        self._subscribers[sub_id] = q
        logger.debug(f"[PREGEN-STREAM] Subscriber added: {sub_id} (filter={story_id})")
        return sub_id, q

    def unsubscribe(self, sub_id: str) -> None:
        """Remove a subscriber."""
        self._subscribers.pop(sub_id, None)
        logger.debug(f"[PREGEN-STREAM] Subscriber removed: {sub_id}")

    def broadcast(self, event: dict) -> None:
        """Broadcast an event to all subscribers.

        Events are filtered per-subscriber if story_id was set.
        queue_update and scheduler_idle are always delivered (no filter).
        """
        event_type = event.get("type", "")
        story_id = event.get("story_id", "")

        for sub_id, q in list(self._subscribers.items()):
            # Apply per-subscriber filter (skip for global events)
            story_filter = getattr(q, '_story_filter', None)
            if story_filter and story_id and story_id != story_filter:
                # Only filter chapter-specific events, not queue_update/idle
                if event_type in ("chapter_generating", "chapter_ready", "chapter_error"):
                    continue
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(f"[PREGEN-STREAM] Queue full for {sub_id}, dropping event")

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# Singleton
pregen_stream_manager = PregenStreamManager()
