"""Regression tests for ``services.inject_resume``.

Guards the auto-wake-orchestrator path that broke production
(b61af7db incident, 2026-05-09): when all team members went idle,
``_trigger_orchestrator_resume`` failed silently with::

    ImportError: cannot import name 'DisplayManager' from
    'fast_agent.spawn.spawn_display'

The class had been renamed to ``SpawnDisplayManager`` but
``inject_resume._create_bridge_display`` was never updated. The exception
was swallowed by ``_trigger_orchestrator_resume``'s try/except, surviving
only as a WARNING in ``spawn_activity.log`` — meanwhile the orchestrator
was never woken to read the team-status notification sitting in its inbox.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def test_create_bridge_display_imports_correct_class():
    """Smoke-test the import path that was stale.

    The bug: ``inject_resume.py`` imported ``DisplayManager`` (renamed to
    ``SpawnDisplayManager``). This test calls ``_create_bridge_display``
    with a mock bridge and asserts that the import + instantiation +
    ``set_event_callback`` chain all succeed.
    """
    from services.inject_resume import _create_bridge_display

    mock_bridge = MagicMock()
    dm = _create_bridge_display(mock_bridge)

    # Verify the right class was imported and instantiated.
    from fast_agent.spawn.spawn_display import SpawnDisplayManager
    assert isinstance(dm, SpawnDisplayManager), (
        f"Expected SpawnDisplayManager, got {type(dm).__name__}"
    )

    # Verify the event callback was attached — without it, child
    # subprocess events never reach the bridge.
    assert dm._event_callback is not None, (
        "set_event_callback never ran — events from resumed agent "
        "would not flow to SpawnProgressBridge"
    )


def test_create_bridge_display_forwards_events_to_bridge():
    """End-to-end: SpawnEvent fired through DM lands in bridge.process_event."""
    import json

    from services.inject_resume import _create_bridge_display

    mock_bridge = MagicMock()
    dm = _create_bridge_display(mock_bridge)

    # Build a SpawnEvent-like object (duck-typed — uses getattr fallbacks).
    fake_event = MagicMock()
    fake_event.agent_name = "Cameron [PM]"
    fake_event.event = "agent_ready"
    fake_event.run_id = "abc12345"
    fake_event.data = {"foo": "bar"}

    dm._event_callback(fake_event)

    mock_bridge.process_event.assert_called_once()
    sent_line = mock_bridge.process_event.call_args[0][0]
    payload = json.loads(sent_line)
    assert payload["agent_name"] == "Cameron [PM]"
    assert payload["event_type"] == "agent_ready"
    assert payload["run_id"] == "abc12345"
    assert payload["data"] == {"foo": "bar"}
