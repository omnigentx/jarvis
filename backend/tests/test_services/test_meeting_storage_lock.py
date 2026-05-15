"""Regression tests for ``SqliteMeetingStorage`` lock behaviour
post-B1 refactor (mutable fields moved into ``state_json``).

History: production hit a self-deadlock on 2026-05-09 (incident
b61af7db) when ``leave_meeting`` called ``update_config`` to mutate
``participants`` while holding ``acquire_lock``. ``update_config``
opened a fresh connection and tried to write — same-process write/write
contention against the outer ``BEGIN IMMEDIATE`` exhausted busy_timeout
and raised ``OperationalError: database is locked``.

The B1 refactor removes the dual-write window structurally: all mutable
fields (participants, max_rounds, current_turn, etc.) live in
``state_json``. ``update_config`` was deleted. There is now exactly one
write path (``update_state``) inside the lock, so the deadlock class is
no longer reachable.

These tests guard the post-B1 contract:

* speak()'s FAIL-verdict path extends ``state.max_rounds`` inside a
  single ``acquire_lock`` without timing out.
* leave_meeting() removes ``state.participants`` inside a single
  ``acquire_lock`` without timing out.
* The deleted ``update_config`` is genuinely gone (so a future commit
  can't reintroduce the bug class by adding a second write path).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


def test_update_config_method_was_removed():
    """Structural guard: update_config must not exist on either storage impl.

    Post-B1, all mutable fields live in state_json. Anyone reintroducing
    update_config likely is also reintroducing the deadlock class —
    fail loudly here so the PR never lands.
    """
    from fast_agent.spawn.servers.meeting_storage import (
        JsonFileMeetingStorage,
        SqliteMeetingStorage,
    )

    assert not hasattr(JsonFileMeetingStorage, "update_config"), (
        "JsonFileMeetingStorage.update_config came back — see B1 refactor "
        "rationale in meeting_storage.py header"
    )
    assert not hasattr(SqliteMeetingStorage, "update_config"), (
        "SqliteMeetingStorage.update_config came back — see B1 refactor "
        "rationale in meeting_storage.py header"
    )


def test_speak_fail_verdict_extends_max_rounds_inside_lock(tmp_path: Path):
    """Integration: speak()'s FAIL-verdict path now mutates state.max_rounds
    inside the single ``acquire_lock`` transaction without deadlock.
    """
    from fast_agent.spawn.message_bus import MessageBus
    from fast_agent.spawn.servers.meeting_storage import SqliteMeetingStorage
    import fast_agent.spawn.servers.meeting_room_server as mrs

    db = SqliteMeetingStorage(db_path=str(tmp_path / "m.db"))
    bus = MessageBus(messages_dir=str(tmp_path / "messages"))
    Path(str(tmp_path / "messages")).mkdir(exist_ok=True)

    # Tight max_rounds + current_round=2 so FAIL extends (remaining < 3).
    db.create_meeting(
        "abc12345",
        config={"agenda": "Test", "created_by": "PM"},
        state={
            "participants": ["PM", "Dev"],
            "max_rounds": 2,
            "current_turn": 0, "current_round": 2,
            "joined": ["PM", "Dev"], "ended": False, "started": True,
            "read_cursors": {},
        },
    )

    import asyncio

    orig_storage = mrs._storage
    orig_get_bus = mrs._get_bus
    orig_wake = mrs._auto_wake_if_idle
    mrs._storage = db
    mrs._get_bus = lambda: bus
    mrs._auto_wake_if_idle = lambda *a, **kw: None

    try:
        start = time.monotonic()
        result = asyncio.run(
            mrs.speak(
                meeting_id="abc12345",
                message="[DECISION] VERDICT: FAIL — needs more analysis",
                agent_name="PM",
            )
        )
        elapsed = time.monotonic() - start
    finally:
        mrs._storage = orig_storage
        mrs._get_bus = orig_get_bus
        mrs._auto_wake_if_idle = orig_wake

    assert elapsed < 2.0, f"speak FAIL path took {elapsed:.2f}s — deadlock?"

    payload = json.loads(result)
    assert payload.get("verdict") == "fail"
    assert payload.get("meeting_ended") is False, "FAIL should NOT end meeting"

    # max_rounds extended (2 → 5) and lives in state_json now.
    assert db.get_state("abc12345")["max_rounds"] == 5
    # config_json must NOT carry max_rounds — would reintroduce dual write.
    assert "max_rounds" not in db.get_config("abc12345")


def test_leave_meeting_inside_lock_completes(tmp_path: Path):
    """Integration: leave_meeting — the exact tool that 32-second-hung in
    production — now completes promptly because participants lives in
    state_json (single update path).
    """
    from fast_agent.spawn.message_bus import MessageBus
    from fast_agent.spawn.servers.meeting_storage import SqliteMeetingStorage
    import fast_agent.spawn.servers.meeting_room_server as mrs

    db = SqliteMeetingStorage(db_path=str(tmp_path / "m.db"))
    bus = MessageBus(messages_dir=str(tmp_path / "messages"))
    Path(str(tmp_path / "messages")).mkdir(exist_ok=True)

    # current_turn=0 → PM is speaker. BA (index 1) leaves as non-current
    # speaker so R3 guard does not trigger. (R3 guard is exercised by
    # ``test_leave_meeting_rejected_for_current_speaker`` below.)
    db.create_meeting(
        "abc12345",
        config={"agenda": "Test", "created_by": "PM"},
        state={
            "participants": ["PM", "BA", "Dev"],
            "max_rounds": 3,
            "current_turn": 0, "current_round": 1,
            "joined": ["PM", "BA", "Dev"], "ended": False, "started": True,
            "read_cursors": {},
        },
    )

    import asyncio

    orig_storage = mrs._storage
    orig_get_bus = mrs._get_bus
    orig_wake = mrs._auto_wake_if_idle
    mrs._storage = db
    mrs._get_bus = lambda: bus
    mrs._auto_wake_if_idle = lambda *a, **kw: None

    try:
        start = time.monotonic()
        result = asyncio.run(
            mrs.leave_meeting(
                meeting_id="abc12345",
                agent_name="BA",
                reason="testing",
            )
        )
        elapsed = time.monotonic() - start
    finally:
        mrs._storage = orig_storage
        mrs._get_bus = orig_get_bus
        mrs._auto_wake_if_idle = orig_wake

    assert elapsed < 2.0, (
        f"leave_meeting took {elapsed:.2f}s — production saw 32s here, "
        f"would mean the B1 refactor regressed"
    )

    payload = json.loads(result)
    assert payload.get("status") == "left"
    assert "BA" not in db.get_state("abc12345")["participants"]
    # config_json must remain untouched (write-once after create).
    assert "participants" not in db.get_config("abc12345")


def test_leave_meeting_rejected_for_current_speaker(tmp_path: Path):
    """R3 guard: leave_meeting MUST refuse when caller is current_speaker.

    The b61af7db incident hung for 24h because BA's LLM picked
    ``leave_meeting`` over ``speak()`` mid-turn, stranding the next
    speaker. This guard forces the LLM back onto the speak/skip/verdict
    protocol rail.
    """
    from fast_agent.spawn.message_bus import MessageBus
    from fast_agent.spawn.servers.meeting_storage import SqliteMeetingStorage
    import fast_agent.spawn.servers.meeting_room_server as mrs

    db = SqliteMeetingStorage(db_path=str(tmp_path / "m.db"))
    bus = MessageBus(messages_dir=str(tmp_path / "messages"))
    Path(str(tmp_path / "messages")).mkdir(exist_ok=True)

    # current_turn=0 → BA is current speaker
    db.create_meeting(
        "abc12345",
        config={"agenda": "Test", "created_by": "PM"},
        state={
            "participants": ["BA", "PM", "Dev"],
            "max_rounds": 3,
            "current_turn": 0, "current_round": 1,
            "joined": ["BA", "PM", "Dev"], "ended": False, "started": True,
            "read_cursors": {},
        },
    )

    import asyncio

    orig_storage, orig_get_bus, orig_wake = mrs._storage, mrs._get_bus, mrs._auto_wake_if_idle
    mrs._storage = db
    mrs._get_bus = lambda: bus
    mrs._auto_wake_if_idle = lambda *a, **kw: None

    try:
        result = asyncio.run(
            mrs.leave_meeting(
                meeting_id="abc12345",
                agent_name="BA",          # ← current_speaker
                reason="going to do work",
            )
        )
    finally:
        mrs._storage = orig_storage
        mrs._get_bus = orig_get_bus
        mrs._auto_wake_if_idle = orig_wake

    payload = json.loads(result)
    assert "error" in payload
    assert "your turn" in payload["error"].lower()
    assert payload["current_speaker"] == "BA"
    assert "speak" in payload["next_action"]
    assert "VERDICT" in payload["next_action"]
    assert "skip_turn" in payload["next_action"]

    # State unchanged — participants list and turn pointer must be intact.
    state = db.get_state("abc12345")
    assert state["participants"] == ["BA", "PM", "Dev"]
    assert state["current_turn"] == 0
    assert not state["ended"]
    # And no "leave" entry written to the transcript.
    assert db.get_transcript("abc12345") == []


def test_leave_meeting_allowed_for_non_current_speaker(tmp_path: Path):
    """R3 guard: non-current speakers can still leave normally."""
    from fast_agent.spawn.message_bus import MessageBus
    from fast_agent.spawn.servers.meeting_storage import SqliteMeetingStorage
    import fast_agent.spawn.servers.meeting_room_server as mrs

    db = SqliteMeetingStorage(db_path=str(tmp_path / "m.db"))
    bus = MessageBus(messages_dir=str(tmp_path / "messages"))
    Path(str(tmp_path / "messages")).mkdir(exist_ok=True)

    # current_turn=0 → BA. Dev is index 2 — safe to leave.
    db.create_meeting(
        "abc12345",
        config={"agenda": "Test", "created_by": "PM"},
        state={
            "participants": ["BA", "PM", "Dev"],
            "max_rounds": 3,
            "current_turn": 0, "current_round": 1,
            "joined": ["BA", "PM", "Dev"], "ended": False, "started": True,
            "read_cursors": {},
        },
    )

    import asyncio

    orig_storage, orig_get_bus, orig_wake = mrs._storage, mrs._get_bus, mrs._auto_wake_if_idle
    mrs._storage = db
    mrs._get_bus = lambda: bus
    mrs._auto_wake_if_idle = lambda *a, **kw: None

    try:
        result = asyncio.run(
            mrs.leave_meeting(
                meeting_id="abc12345",
                agent_name="Dev",
                reason="reassigned",
            )
        )
    finally:
        mrs._storage = orig_storage
        mrs._get_bus = orig_get_bus
        mrs._auto_wake_if_idle = orig_wake

    assert json.loads(result)["status"] == "left"
    assert "Dev" not in db.get_state("abc12345")["participants"]


def test_add_participant_inside_lock_completes(tmp_path: Path):
    """Integration: add_participant uses the same single-write path.

    Pre-B1 this also called update_config (would have shown the deadlock
    on second invocation under load). Now it's a state-only update.
    """
    from fast_agent.spawn.message_bus import MessageBus
    from fast_agent.spawn.servers.meeting_storage import SqliteMeetingStorage
    import fast_agent.spawn.servers.meeting_room_server as mrs

    db = SqliteMeetingStorage(db_path=str(tmp_path / "m.db"))
    bus = MessageBus(messages_dir=str(tmp_path / "messages"))
    Path(str(tmp_path / "messages")).mkdir(exist_ok=True)

    db.create_meeting(
        "abc12345",
        config={"agenda": "Test", "created_by": "PM"},
        state={
            "participants": ["PM", "Dev"],
            "max_rounds": 3,
            "current_turn": 0, "current_round": 1,
            "joined": ["PM", "Dev"], "ended": False, "started": True,
            "read_cursors": {},
        },
    )

    import asyncio

    orig_storage = mrs._storage
    orig_get_bus = mrs._get_bus
    orig_wake = mrs._auto_wake_if_idle
    orig_my_name = mrs._get_my_name
    mrs._storage = db
    mrs._get_bus = lambda: bus
    mrs._auto_wake_if_idle = lambda *a, **kw: None
    mrs._get_my_name = lambda: "PM"

    try:
        start = time.monotonic()
        result = asyncio.run(
            mrs.add_participant(meeting_id="abc12345", agent_name="QE")
        )
        elapsed = time.monotonic() - start
    finally:
        mrs._storage = orig_storage
        mrs._get_bus = orig_get_bus
        mrs._auto_wake_if_idle = orig_wake
        mrs._get_my_name = orig_my_name

    assert elapsed < 2.0
    payload = json.loads(result)
    assert payload.get("status") == "added"

    state = db.get_state("abc12345")
    assert "QE" in state["participants"]
    assert "QE" in state["joined"]
