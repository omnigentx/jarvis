"""Push credential invalidation through the existing activity event pipeline."""

import logging
import os


def notify_remote_disconnect(candidate: str, server: str):
    data = {"id": candidate, "remote_server": server, "remote_status": "disconnected"}
    try:
        if os.environ.get("SPAWN_EVENT_SOCKET"):
            from fast_agent.spawn.spawn_events import emit_event

            emit_event("plugin_status", "", "", **data)
        else:
            from services.activity_stream import activity_stream_manager

            activity_stream_manager.broadcast(
                {"event_type": "plugin_status", "agent_name": "", "data": data}
            )
    except Exception as exc:
        # State is already durable. A reconnect fetches it even if push failed.
        logging.getLogger(__name__).warning(
            "[plugins] Remote status push failed candidate=%s type=%s",
            candidate,
            type(exc).__name__,
        )
