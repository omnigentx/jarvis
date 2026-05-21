"""Regression tests for the multi-signal status derivation
(``routes/agents.py::_compute_effective_status``).

Background: ``spawn_registry.status`` is updated only by the spawn-event
bridge. If the bridge socket dies (R1/R2 race) or the subprocess is
SIGKILL'd before emitting its terminal event, the value can stay frozen
at ``"running"`` indefinitely. The UI then shows agents as ``running``
forever even though they are idle or have exited.

The helper cross-references three independent signals:

  1. **Channel sock probe** — connect-test against the agent's Unix
     socket. Alive ⇒ subprocess is in keep-alive ``listen()``.
  2. **Latest snapshot** — ``trigger`` AND ``created_at`` the subprocess
     wrote directly to SQLite. Bypasses the bridge entirely.
  3. **``record.last_active_at``** — bridge-fed timestamp of the agent's
     last LLM-side event. Used to distinguish a fresh idle-snapshot
     from a stale one captured before a new turn started.

Lifecycle invariant enforced everywhere else (see
``spawn_progress_bridge`` line ~607, ``isolated_spawner`` line ~932,
``agent_registry_db.mark_stale_running`` line ~284): for a dead-channel
agent that last wrote idle/task_complete, **resumable → "idle"**
(respawnable on demand), **oneshot → "completed"** (one-and-done).
This file pins that invariant for the inferred-state path too.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Make routes/ importable without running the full FastAPI app.
_BACKEND = Path(__file__).parent.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from routes.agents import _compute_effective_status  # noqa: E402


# ─── Helpers ──────────────────────────────────────────────────────────


def _make_snapshot_db(
    tmp_path: Path,
    agent_name: str,
    trigger: str,
    created_at: float | None = None,
) -> str:
    """Create a tiny SQLite DB with a single ``agent_context_snapshots``
    row for ``agent_name``. ``trigger`` and ``created_at`` go in
    verbatim. Returns the DB path string.
    """
    db = tmp_path / "snapshots.db"
    conn = sqlite3.connect(db)
    # IF NOT EXISTS so the helper can be called multiple times in one
    # test (parametrize loops, multi-case sanity tests) against the
    # same tmp_path. Each call appends a new row with a unique id.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_context_snapshots ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT, "
        "trigger TEXT, created_at REAL)"
    )
    conn.execute(
        "INSERT INTO agent_context_snapshots "
        "(agent_name, trigger, created_at) VALUES (?, ?, ?)",
        (agent_name, trigger, created_at if created_at is not None else 1.0),
    )
    conn.commit()
    conn.close()
    return str(db)


# ─── Trust-as-is: terminal / overlay statuses pass through unchanged ─


@pytest.mark.parametrize("terminal", [
    "completed", "error", "killed", "failed", "cancelled", "paused", "timeout",
])
def test_terminal_status_passes_through(terminal):
    """Once an agent is in a terminal/overlay state, the helper must
    NOT second-guess it. Bridge / cleanup / PauseManager are
    authoritative for these.

    Note: the ``completed`` + non-oneshot lifecycle case is exempted —
    see ``test_completed_resumable_record_overrides_to_idle``.
    """
    record = {"agent_name": "X", "status": terminal}
    assert _compute_effective_status(record) == terminal


def test_completed_resumable_record_overrides_to_idle():
    """Post-2026-05-20 lifecycle merge: legacy DB rows had
    ``status="completed"`` written by the old spawner for what is now a
    ``resumable`` agent. The canonical state for a non-oneshot agent
    after task completion is ``idle`` (waiting for follow-up). Override
    at the API layer so UI doesn't render a misleading "Completed" badge
    while the same agent is still resumable from inbox or resume_spawn.
    """
    record = {"agent_name": "X", "status": "completed", "lifecycle": "resumable"}
    assert _compute_effective_status(record) == "idle"


def test_completed_oneshot_record_stays_completed():
    """Oneshot agents really ARE done — the override must not touch them."""
    record = {"agent_name": "X", "status": "completed", "lifecycle": "oneshot"}
    assert _compute_effective_status(record) == "completed"


def test_unrecognized_status_passes_through():
    """A future status value the helper doesn't know about returns as-is.
    Lets the codebase introduce new statuses without surgery here.
    """
    record = {"agent_name": "X", "status": "spawning_v2_experimental"}
    assert _compute_effective_status(record) == "spawning_v2_experimental"


# ─── Channel alive (subprocess in keep-alive) ────────────────────────


def test_channel_alive_idle_snapshot_newer_than_last_active_returns_idle(
    tmp_path: Path,
):
    """The canonical "agent finished a turn, sitting idle" case.

    Subprocess wrote ``idle`` snapshot at t=100 AFTER the bridge last
    saw activity at t=50. Snapshot is fresh ⇒ subprocess is really idle.
    UI must NOT show ``running`` here.
    """
    db = _make_snapshot_db(tmp_path, "Sasha", "task_complete", created_at=100.0)
    record = {"agent_name": "Sasha", "status": "running", "last_active_at": 50.0}
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=True):
        assert _compute_effective_status(record, snapshots_db_path=db) == "idle"


def test_channel_alive_idle_snapshot_no_last_active_returns_idle(tmp_path: Path):
    """When ``last_active_at`` is missing (first turn, or pre-R2 record),
    a present-and-correct idle snapshot is enough to conclude idle.
    """
    db = _make_snapshot_db(tmp_path, "Sasha", "idle", created_at=100.0)
    record = {"agent_name": "Sasha", "status": "running"}  # no last_active_at
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=True):
        assert _compute_effective_status(record, snapshots_db_path=db) == "idle"


def test_channel_alive_stale_snapshot_during_active_turn_keeps_raw(tmp_path: Path):
    """Critical mid-turn case. Snapshot ``task_complete`` is from the
    PREVIOUS turn (t=50). A new turn has started and the bridge has
    bumped ``last_active_at`` to t=100. The fresh activity timestamp
    proves the snapshot is stale → trust raw ``running``.

    Without this check the UI would falsely flip a working agent to
    ``idle`` between snapshot writes, hiding mid-LLM-call activity.
    """
    db = _make_snapshot_db(tmp_path, "Sasha", "task_complete", created_at=50.0)
    record = {"agent_name": "Sasha", "status": "running", "last_active_at": 100.0}
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=True):
        assert _compute_effective_status(record, snapshots_db_path=db) == "running"


def test_channel_alive_no_snapshot_keeps_raw(tmp_path: Path):
    """No snapshot for this agent yet — likely first turn in progress.
    Keep raw ``running``; don't guess idle without evidence.
    """
    db = _make_snapshot_db(tmp_path, "OtherAgent", "task_complete")
    record = {"agent_name": "Sasha", "status": "running"}
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=True):
        assert _compute_effective_status(record, snapshots_db_path=db) == "running"


def test_channel_alive_unrelated_trigger_keeps_raw(tmp_path: Path):
    """Snapshot trigger is something other than idle/task_complete
    (e.g. ``error`` from a prior turn that the agent has since
    recovered from). Don't override raw based on that — wait for a
    fresh idle snapshot.
    """
    db = _make_snapshot_db(tmp_path, "Sasha", "tool_call_recovered")
    record = {"agent_name": "Sasha", "status": "running"}
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=True):
        assert _compute_effective_status(record, snapshots_db_path=db) == "running"


# ─── Channel dead (subprocess exited) ────────────────────────────────


def test_channel_dead_oneshot_task_complete_returns_completed(tmp_path: Path):
    """Oneshot agent ran its single task, wrote ``task_complete``,
    subprocess exited. UI must reflect ``completed`` — no further
    activity expected.
    """
    db = _make_snapshot_db(tmp_path, "Sasha", "task_complete")
    record = {"agent_name": "Sasha", "status": "running", "lifecycle": "oneshot"}
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=False):
        assert _compute_effective_status(record, snapshots_db_path=db) == "completed"


def test_channel_dead_oneshot_idle_trigger_returns_completed(tmp_path: Path):
    """Oneshot whose last snapshot was ``idle`` (rare but possible if
    cleanup wrote idle before final exit). Still ``completed`` —
    oneshot is one-and-done regardless of last-snapshot label.
    """
    db = _make_snapshot_db(tmp_path, "Sasha", "idle")
    record = {"agent_name": "Sasha", "status": "running", "lifecycle": "oneshot"}
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=False):
        assert _compute_effective_status(record, snapshots_db_path=db) == "completed"


def test_channel_dead_resumable_idle_trigger_returns_idle(tmp_path: Path):
    """The whole reason the helper got rewritten on 2026-05-13.

    Resumable team agent: subprocess exited (backend restart, SIGKILL,
    or normal hibernation flow) after writing ``idle``. The agent is
    NOT terminal — ``auto_wake_if_idle`` will respawn it from snapshot
    on the next inbound message. UI must show ``idle`` so the user
    (and other agents) treat it as available, matching the canonical
    rule already used by ``spawn_progress_bridge`` and
    ``mark_stale_running``.

    Previously this branch returned ``completed`` and the dashboard
    silently hid 7 resumable team agents that should have stayed
    visible as ``idle``.
    """
    db = _make_snapshot_db(tmp_path, "Sasha", "idle")
    record = {"agent_name": "Sasha", "status": "running", "lifecycle": "resumable"}
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=False):
        assert _compute_effective_status(record, snapshots_db_path=db) == "idle"


def test_channel_dead_resumable_task_complete_returns_idle(tmp_path: Path):
    """Resumable agents finish many tasks across their lifetime;
    ``task_complete`` means "current task done" not "agent done". Still
    respawnable → ``idle``.
    """
    db = _make_snapshot_db(tmp_path, "Sasha", "task_complete")
    record = {"agent_name": "Sasha", "status": "running", "lifecycle": "resumable"}
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=False):
        assert _compute_effective_status(record, snapshots_db_path=db) == "idle"


def test_channel_dead_missing_lifecycle_defaults_to_idle(tmp_path: Path):
    """Defensive: missing/empty ``lifecycle`` field (pre-R2 records, or
    records written before the field existed). Default to the safer
    ``idle`` — keeps the agent visible and treats it as respawnable.
    Defaulting to ``completed`` would prematurely hide agents from the
    dashboard.
    """
    db = _make_snapshot_db(tmp_path, "Sasha", "idle")
    record = {"agent_name": "Sasha", "status": "running"}  # no lifecycle
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=False):
        assert _compute_effective_status(record, snapshots_db_path=db) == "idle"


def test_channel_dead_persistent_lifecycle_returns_idle(tmp_path: Path):
    """The ``persistent`` lifecycle (defined in the enum, not heavily
    used yet) — should behave like resumable for the idle case
    (long-running, manual cleanup). Use ``idle`` not ``completed``.
    """
    db = _make_snapshot_db(tmp_path, "Sasha", "idle")
    record = {"agent_name": "Sasha", "status": "running", "lifecycle": "persistent"}
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=False):
        assert _compute_effective_status(record, snapshots_db_path=db) == "idle"


def test_channel_dead_and_error_trigger_returns_error(tmp_path: Path):
    """Subprocess died after writing an error snapshot — regardless of
    lifecycle, ``error`` is the right terminal label.
    """
    db = _make_snapshot_db(tmp_path, "Sasha", "error")
    record = {"agent_name": "Sasha", "status": "running", "lifecycle": "resumable"}
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=False):
        assert _compute_effective_status(record, snapshots_db_path=db) == "error"


def test_channel_dead_no_snapshot_returns_raw(tmp_path: Path):
    """SIGKILL'd before any snapshot was written — OR — fresh-spawn
    race where channel hasn't bound yet. These are indistinguishable
    from a single probe.

    Previously this returned ``completed_unknown`` (a NEW state not
    in the canonical ``SpawnStatus`` enum, unknown to the frontend
    and to bridge logic). We now return raw and let
    ``mark_stale_running`` reconcile on the next backend restart —
    which is what was already happening pre-helper.
    """
    db = _make_snapshot_db(tmp_path, "OtherAgent", "task_complete")
    record = {"agent_name": "Sasha", "status": "running", "lifecycle": "resumable"}
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=False):
        assert _compute_effective_status(record, snapshots_db_path=db) == "running"


# ─── Probe failures: fall back gracefully ────────────────────────────


def test_probe_exception_falls_back_to_raw_status(tmp_path: Path):
    """If the channel probe throws (import error, OS error), don't
    invent a status — return raw. Better to display a potentially
    stale ``running`` than to randomly guess.
    """
    db = _make_snapshot_db(tmp_path, "Sasha", "task_complete")
    record = {"agent_name": "Sasha", "status": "running"}
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               side_effect=RuntimeError("probe broken")):
        assert _compute_effective_status(record, snapshots_db_path=db) == "running"


def test_missing_agent_name_returns_raw():
    """Defensive: record with no agent_name — can't probe, return raw."""
    record = {"status": "running"}
    assert _compute_effective_status(record) == "running"


def test_no_snapshot_db_returns_raw_for_dead_channel():
    """When the DB path is None (e.g. snapshot DB not yet created in
    a fresh project), there is no trigger to read. Channel dead +
    no trigger → trust raw. Previously returned ``completed_unknown``
    which leaked a non-canonical value to the UI.
    """
    record = {"agent_name": "Sasha", "status": "running", "lifecycle": "resumable"}
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=False):
        assert _compute_effective_status(record, snapshots_db_path=None) == "running"


# ─── Mutable-status set: pending / starting / resumed / idle ─────────


@pytest.mark.parametrize("raw", ["running", "starting", "resumed", "pending", "idle", "unknown"])
def test_mutable_raw_states_are_evaluated_by_helper(raw, tmp_path: Path):
    """All values in the mutable set go through the decision tree.
    A resumable+dead+idle-snapshot must resolve to ``idle`` regardless
    of which mutable raw state the bridge happened to write last.
    """
    db = _make_snapshot_db(tmp_path, "Sasha", "idle")
    record = {"agent_name": "Sasha", "status": raw, "lifecycle": "resumable"}
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
               return_value=False):
        assert _compute_effective_status(record, snapshots_db_path=db) == "idle"


# ─── Sanity: helper never returns the deprecated ``completed_unknown`` ──


def test_helper_never_returns_completed_unknown(tmp_path: Path):
    """Pin the contract: ``completed_unknown`` was a value the helper
    once invented; it is NOT in ``SpawnStatus`` enum, bridge,
    cleanup, or frontend. The helper must never resurface it.
    """
    forbidden = "completed_unknown"
    cases = [
        ({"agent_name": "A", "status": "running", "lifecycle": "resumable"}, False, None),
        ({"agent_name": "A", "status": "running", "lifecycle": "oneshot"}, False, None),
        ({"agent_name": "A", "status": "running"}, False, None),
        ({"agent_name": "A", "status": "running"}, True, "task_complete"),
        ({"agent_name": "A", "status": "running"}, True, None),
    ]
    for record, alive, trigger in cases:
        db = _make_snapshot_db(tmp_path, "A", trigger or "noop")
        with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive",
                   return_value=alive):
            out = _compute_effective_status(record, snapshots_db_path=db)
            assert out != forbidden, (
                f"Helper resurfaced deprecated value for case "
                f"alive={alive}, trigger={trigger}, record={record}: {out!r}"
            )
