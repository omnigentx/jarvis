"""Regression tests for the R2 ``last_active_at`` visibility fix.

Background (b61af7db incident, 2026-05-09): PM polled
``list_active_spawns`` / ``get_team_status`` twice and saw identical
``status: running`` snapshots. Without any signal of *progress*, PM
concluded "status UNCHANGED" and idled out — burying the team
mid-meeting. The spawn_registry only carried ``started_at`` /
``completed_at``; there was nothing to distinguish "agent is grinding
through an LLM call right now" from "agent has been silent for 60s".

R2 fix: every ``thinking``/``response``/``tool_call``/``tool_result``
event upserts ``last_active_at = now()`` on the agent's spawn_registry
record. ``get_team_status`` exposes both the raw timestamp and a
``stuck_seconds`` indicator when the gap exceeds 30s.

These tests guard:
* the bridge writes ``last_active_at`` on activity events;
* the bridge does NOT touch ``last_active_at`` on lifecycle-only events
  (started/idle/agent_completed) — those are status changes, not work;
* the SpawnRecord dataclass round-trips the new field through to/from_dict.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest


# ─── SpawnRecord field round-trip ──────────────────────────────────


def test_spawn_record_persists_last_active_at():
    """SpawnRecord.from_dict(to_dict(...)) preserves last_active_at.

    If this regresses, ``get_latest()`` would silently drop the field
    when reconstructing the record from JSON storage.
    """
    from fast_agent.spawn.spawn_registry import SpawnRecord

    rec = SpawnRecord(run_id="r1", agent_name="X", last_active_at=1234567.5)
    round_tripped = SpawnRecord.from_dict(rec.to_dict())
    assert round_tripped.last_active_at == 1234567.5


def test_spawn_record_default_last_active_at_is_none():
    """Default value: None — distinguishes "never active" from "active at t=0"."""
    from fast_agent.spawn.spawn_registry import SpawnRecord

    rec = SpawnRecord(run_id="r1", agent_name="X")
    assert rec.last_active_at is None


# ─── Bridge updates last_active_at on activity events ──────────────


@pytest.fixture
def bridge_with_mock_registry():
    """SpawnProgressBridge wired to a mock registry_db that records upserts."""
    from services.spawn_progress_bridge import SpawnProgressBridge

    mock_registry = MagicMock()
    bridge = SpawnProgressBridge(
        progress_manager=MagicMock(), registry_db=mock_registry,
    )
    return bridge, mock_registry


@pytest.mark.parametrize("event_type", ["thinking", "response", "tool_call", "tool_result"])
def test_bridge_updates_last_active_on_activity_events(
    bridge_with_mock_registry, event_type,
):
    """Each activity event upserts last_active_at with a fresh timestamp."""
    bridge, mock_registry = bridge_with_mock_registry
    mock_registry.get_record.return_value = {"run_id": "r1"}

    line = json.dumps({
        "agent_name": "BA",
        "event_type": event_type,
        "run_id": "r1",
        "data": {},
    })

    before = time.time()
    bridge.process_event(line)
    after = time.time()

    # Find the upsert call that carried last_active_at (other upserts
    # may happen too via _upsert_spawn_record).
    last_active_calls = [
        c for c in mock_registry.upsert_record.call_args_list
        if "last_active_at" in (c.args[1] if len(c.args) > 1 else {})
    ]
    assert len(last_active_calls) >= 1, (
        f"Expected ≥1 upsert with last_active_at for event {event_type!r}, "
        f"got calls: {mock_registry.upsert_record.call_args_list}"
    )
    ts = last_active_calls[-1].args[1]["last_active_at"]
    assert before <= ts <= after, (
        f"last_active_at ({ts}) must fall within [{before}, {after}]"
    )


@pytest.mark.parametrize("event_type", ["started", "idle", "agent_completed", "lifecycle_registered"])
def test_bridge_does_not_update_last_active_on_lifecycle_events(
    bridge_with_mock_registry, event_type,
):
    """Lifecycle-only events MUST NOT bump last_active_at — they're status
    changes, not work. Otherwise an "idle" event would falsely look like
    activity and mask the real stall.
    """
    bridge, mock_registry = bridge_with_mock_registry
    mock_registry.get_record.return_value = {"run_id": "r1"}

    line = json.dumps({
        "agent_name": "BA",
        "event_type": event_type,
        "run_id": "r1",
        "data": {},
    })
    bridge.process_event(line)

    last_active_calls = [
        c for c in mock_registry.upsert_record.call_args_list
        if "last_active_at" in (c.args[1] if len(c.args) > 1 else {})
    ]
    assert last_active_calls == [], (
        f"Lifecycle event {event_type!r} should NOT touch last_active_at, "
        f"but got: {last_active_calls}"
    )


def test_bridge_skips_last_active_when_no_run_id(bridge_with_mock_registry):
    """Defensive: missing run_id → no upsert, no crash."""
    bridge, mock_registry = bridge_with_mock_registry

    line = json.dumps({"agent_name": "BA", "event_type": "thinking", "data": {}})
    bridge.process_event(line)

    last_active_calls = [
        c for c in mock_registry.upsert_record.call_args_list
        if "last_active_at" in (c.args[1] if len(c.args) > 1 else {})
    ]
    assert last_active_calls == []
