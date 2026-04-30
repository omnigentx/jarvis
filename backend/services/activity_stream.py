"""
Activity Stream Manager — SSE broadcast for agent activity events.

Provides a global broadcast mechanism for agent events, independent of
the per-request ProgressEventManager used in chat-stream.

Architecture:
  - ActivityStreamManager manages a set of subscriber asyncio.Queues
  - SpawnProgressBridge calls broadcast() when processing events
  - SSE endpoint yields events from subscriber's queue
"""

import asyncio
import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class ActivityStreamManager:
    """Manages SSE subscribers for agent activity events.
    
    Each subscriber (SSE client) gets its own asyncio.Queue.
    broadcast() fans out events to all active subscribers.
    """
    
    def __init__(self):
        self._subscribers: dict[str, asyncio.Queue] = {}
        self._counter = 0
    
    def subscribe(self, agent_filter: Optional[str] = None) -> tuple[str, asyncio.Queue]:
        """Create a new subscriber. Returns (subscriber_id, queue).
        
        Args:
            agent_filter: If set, only events for this agent_name are delivered.
        """
        self._counter += 1
        sub_id = f"sub_{self._counter}_{int(time.time())}"
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers[sub_id] = q
        # Store filter as queue attribute
        q._agent_filter = agent_filter  # type: ignore[attr-defined]
        logger.info(f"Activity stream subscriber added: {sub_id} (filter={agent_filter})")
        return sub_id, q
    
    def unsubscribe(self, sub_id: str) -> None:
        """Remove a subscriber."""
        self._subscribers.pop(sub_id, None)
        logger.info(f"Activity stream subscriber removed: {sub_id}")
    
    def broadcast(self, event: dict) -> None:
        """Broadcast an event to all subscribers.
        
        Events are filtered per-subscriber if agent_filter was set.
        """
        agent_name = event.get("agent_name", "")
        for sub_id, q in list(self._subscribers.items()):
            # Apply per-subscriber filter
            agent_filter = getattr(q, '_agent_filter', None)
            if agent_filter and agent_name != agent_filter:
                continue
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(f"Activity stream queue full for {sub_id}, dropping event")
    
    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# Singleton
activity_stream_manager = ActivityStreamManager()
