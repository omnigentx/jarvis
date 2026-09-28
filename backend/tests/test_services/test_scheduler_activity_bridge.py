"""Global notifications share the activity stream without losing scheduler events."""

from services.activity_stream import activity_stream_manager
from services.cron_scheduler import scheduler_stream_manager


def test_notification_is_forwarded_to_both_streams() -> None:
    activity_id, activity = activity_stream_manager.subscribe()
    scheduler_id, scheduler = scheduler_stream_manager.subscribe()
    try:
        notification = {"type": "new_notification", "id": "n1", "title": "Done"}
        scheduler_stream_manager.broadcast(notification)

        assert scheduler.get_nowait() == notification
        assert activity.get_nowait() == {
            **notification,
            "event_type": "scheduler_notification",
        }

        scheduler_stream_manager.broadcast({"type": "job_started", "id": "j1"})
        assert scheduler.get_nowait() == {"type": "job_started", "id": "j1"}
        assert activity.empty()
    finally:
        scheduler_stream_manager.unsubscribe(scheduler_id)
        activity_stream_manager.unsubscribe(activity_id)
