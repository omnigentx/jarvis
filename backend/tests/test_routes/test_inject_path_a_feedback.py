"""Regression tests for inject Path A (MessageBus) UI feedback.

2026-05-15 incident: user injected a prompt at Bailey [PM] via the
dashboard. The agent's process was alive (Path A — MessageBus queue
delivery), and the inject API returned ``status=queued`` in ~50ms.

But the user saw no visual feedback in the AgentTerminal:
  - Agent status stayed at "idle" → no pulse dot, no "Running" label.
  - The injected message sat unread in the inbox indefinitely.
  - Eventually the agent did process it (after an unrelated wake),
    but for several minutes the UI was completely silent.

Two contracts pinned by this file:

  1. Path A MUST broadcast a ``started`` activity-stream event so the
     dashboard reflects "running" immediately on submit (parity with
     Path B and Path C which already do this).

  2. Path A MUST call ``auto_wake_if_idle(agent_name)`` so the alive
     agent picks up the inbox message NOW via the AgentChannel socket
     signal — instead of waiting indefinitely for some other event to
     trigger an LLM call (and thus an inbox check inside
     ``InboxWatcherHook.before_llm_call``).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_path_a_broadcasts_started_event_immediately():
    """The inject Path A handler must broadcast a ``started`` event right
    after queueing the message, so the dashboard's agent-status badge
    flips from idle → running BEFORE the user looks for feedback.

    Pinned because previously only Path B (resume) and Path C (generate)
    broadcasted ``started``; Path A was silent, leaving the UI stuck on
    ``idle`` for the full duration between submit and the agent's first
    ``thinking`` event.
    """
    from routes.inject import _inject_via_message_bus

    spawn_record = {
        "agent_name": "Bailey [PM]",
        "session_id": "test-sid",
        "workspace": "",  # force the fallback path that uses session_id
        "status": "idle",
    }

    broadcasted: list[dict] = []

    def _capture(event):
        broadcasted.append(event)

    # Patch the modules ``_inject_via_message_bus`` actually imports
    # from to avoid hitting real MessageBus / activity_stream / wake.
    with patch("routes.inject.activity_stream_manager") as mock_asm, \
            patch("fast_agent.spawn.message_bus.MessageBus") as mock_bus_cls, \
            patch("fast_agent.spawn.servers._team_helpers.auto_wake_if_idle") as mock_wake, \
            patch.dict("os.environ", {"SPAWN_PROJECT_DIR": "/tmp/fake-project"}):
        mock_asm.broadcast.side_effect = _capture
        mock_bus = MagicMock()
        mock_bus_cls.return_value = mock_bus

        result = await _inject_via_message_bus(
            agent_name="Bailey [PM]",
            message="please advance the retro meeting",
            spawn_record=spawn_record,
        )

    # API returns queued (Path A characteristic).
    assert result.status == "queued"
    assert result.path == "message_bus"

    # Wake signal must fire so the alive agent picks up the inbox.
    assert mock_wake.called, (
        "auto_wake_if_idle was NOT called. Without it the agent's "
        "InboxWatcherHook only sees the message on the next "
        "before_llm_call tick — which never fires while the agent "
        "is idle. The inject would sit unread indefinitely."
    )
    assert mock_wake.call_args[0][0] == "Bailey [PM]"

    # Started event must be broadcast so the UI flips status.
    started = [e for e in broadcasted if e.get("event_type") == "started"]
    assert started, (
        "No ``started`` event broadcast. Frontend store only flips "
        "agent.status to 'running' on started/thinking/tool_call/resumed "
        "events. Without this, the dashboard shows no feedback after "
        "the inject API returns."
    )
    assert started[0]["agent_name"] == "Bailey [PM]"
    assert "Processing inject" in started[0]["message"]


@pytest.mark.asyncio
async def test_path_a_inject_does_not_fail_when_wake_raises():
    """``auto_wake_if_idle`` is best-effort — if it raises (e.g. registry
    unavailable in a degraded environment), the inject API call must
    still succeed because the message is already queued in the inbox.

    A regression here would mean transient wake-path failures cause
    HTTP 500 responses for the user even though the underlying
    delivery was fine.
    """
    from routes.inject import _inject_via_message_bus

    spawn_record = {
        "agent_name": "Bailey [PM]",
        "session_id": "test-sid",
        "workspace": "",
    }

    with patch("routes.inject.activity_stream_manager"), \
            patch("fast_agent.spawn.message_bus.MessageBus"), \
            patch(
                "fast_agent.spawn.servers._team_helpers.auto_wake_if_idle",
                side_effect=RuntimeError("registry not loaded"),
            ), \
            patch.dict("os.environ", {"SPAWN_PROJECT_DIR": "/tmp/fake-project"}):
        # Should NOT raise — wake failure is logged and swallowed.
        result = await _inject_via_message_bus(
            agent_name="Bailey [PM]",
            message="ping",
            spawn_record=spawn_record,
        )

    assert result.status == "queued", (
        "Wake failure should not block the inject — the message is "
        "already in the inbox and will be picked up on the next agent "
        "trigger. Inject API must still return queued."
    )
