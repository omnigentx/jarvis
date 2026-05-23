"""Regression tests for the meeting-aware team-completion notifications.

Background (b61af7db incident, 2026-05-09): when a team idled mid-meeting
the orchestrator received two misleading "team finished" messages:

* MessageBus inbox: "📋 Team Status Update — All members have finished. |
  Reese [Dev] | ✅ idle | No output |..."
* DB notification: "✅ Team tools-audit-team completed (4 agents) — No
  detailed result from orchestrator."

Both fired purely off spawn_registry status (idle ⇒ "finished"). They
ignored the open meeting in jarvis.db where the team was actually stuck
waiting for a speaker. The PM, seeing the misleading "all finished"
verdict, gave up — burying the meeting transcript and stalling the work.

These tests guard the post-fix contract:

* ``_active_meetings_with_members`` correctly identifies open meetings
  whose ``state.participants`` overlap with given member names.
* ``_create_team_notification`` reframes title/content when active
  meetings exist.
* The MessageBus inbox path uses the reframed header too.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


# ─── Helpers ────────────────────────────────────────────────────────


def _seed_meeting(db_path: Path, *, meeting_id: str, participants: list[str],
                  ended: bool, current_turn: int = 0, max_rounds: int = 3,
                  agenda: str = "Test agenda") -> None:
    """Insert one meeting row + a few transcript turns for last-3 preview."""
    import sqlite3 as _sqlite3

    from fast_agent.spawn.servers.meeting_storage import SqliteMeetingStorage

    # Initialise schema via storage (idempotent CREATE TABLE).
    SqliteMeetingStorage(db_path=str(db_path))

    config = {
        "agenda": agenda,
        "created_by": participants[0] if participants else "PM",
        "created_at": "2026-05-09T23:04:43",
    }
    state = {
        "participants": participants,
        "max_rounds": max_rounds,
        "current_turn": current_turn,
        "current_round": 1,
        "joined": list(participants),
        "ended": ended,
        "outcome": None,
        "started": True,
        "read_cursors": {},
        "turn_started_at": time.time() - 60,  # last action ~60s ago
    }

    with _sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meetings (meeting_id, config_json, state_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (meeting_id, json.dumps(config), json.dumps(state), time.time()),
        )
        for i, p in enumerate(participants[:3], start=1):
            conn.execute(
                "INSERT INTO meeting_transcripts "
                "(meeting_id, turn, round, agent, message, type, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (meeting_id, i, 1, p, f"{p} message {i}", "speak", time.time()),
            )
        conn.commit()


@pytest.fixture
def env_db(tmp_path, monkeypatch):
    """Point SPAWN_REGISTRY_DB at a temp DB and yield its path."""
    db_path = tmp_path / "jarvis.db"
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(db_path))
    return db_path


# ─── _active_meetings_with_members ──────────────────────────────────


def test_active_meetings_finds_overlap(env_db):
    """Meeting with overlapping participants is returned with last-3 turns."""
    _seed_meeting(
        env_db, meeting_id="m_open",
        participants=["PM", "BA", "Dev"], ended=False,
        agenda="Sprint kickoff",
    )

    from services.spawn_progress_bridge import SpawnProgressBridge

    matches = SpawnProgressBridge._active_meetings_with_members({"BA", "QE"})
    assert len(matches) == 1
    m = matches[0]
    assert m["meeting_id"] == "m_open"
    assert m["agenda"] == "Sprint kickoff"
    assert m["current_speaker"] == "PM"  # current_turn=0 → participants[0]
    assert m["last_action_ago_sec"] is not None
    assert m["last_action_ago_sec"] >= 0
    assert len(m["last_3_turns"]) == 3
    assert m["last_3_turns"][0]["agent"] == "PM"


def test_active_meetings_skips_ended(env_db):
    """Ended meetings are excluded — they're done, not stalled."""
    _seed_meeting(
        env_db, meeting_id="m_done",
        participants=["PM", "BA"], ended=True,
    )

    from services.spawn_progress_bridge import SpawnProgressBridge

    assert SpawnProgressBridge._active_meetings_with_members({"PM", "BA"}) == []


def test_active_meetings_skips_no_overlap(env_db):
    """Meeting with disjoint participants is irrelevant to this team."""
    _seed_meeting(
        env_db, meeting_id="m_other",
        participants=["X", "Y"], ended=False,
    )

    from services.spawn_progress_bridge import SpawnProgressBridge

    assert SpawnProgressBridge._active_meetings_with_members({"PM", "BA"}) == []


def test_active_meetings_empty_member_set_returns_empty(env_db):
    """Defensive: empty input must short-circuit (no DB scan)."""
    from services.spawn_progress_bridge import SpawnProgressBridge

    assert SpawnProgressBridge._active_meetings_with_members(set()) == []


def test_active_meetings_handles_missing_db(tmp_path, monkeypatch):
    """If the DB doesn't exist yet, return [] without crashing."""
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(tmp_path / "nope.db"))

    from services.spawn_progress_bridge import SpawnProgressBridge

    assert SpawnProgressBridge._active_meetings_with_members({"PM"}) == []


# ─── _format_active_meetings_warning ────────────────────────────────


def test_format_warning_includes_bottleneck_and_turns(env_db):
    """The warning block must surface bottleneck + last 3 turns + action hint."""
    _seed_meeting(
        env_db, meeting_id="m_open",
        participants=["PM", "BA", "Dev"], ended=False,
        agenda="Audit kickoff",
    )

    from services.spawn_progress_bridge import SpawnProgressBridge

    matches = SpawnProgressBridge._active_meetings_with_members({"BA"})
    block = SpawnProgressBridge._format_active_meetings_warning(matches)

    assert "Active meetings detected" in block
    assert "m_open" in block
    assert "PM" in block  # bottleneck speaker
    assert "Audit kickoff" in block
    # Last 3 turns rendered with [Agent Rn] preview
    assert "[PM R1]" in block or "[BA R1]" in block
    # Action hint helps PM know what to do
    assert "leave_meeting" in block or "verdict" in block


def test_format_warning_empty_when_no_active(env_db):
    """No active meetings → empty string (no spurious section)."""
    from services.spawn_progress_bridge import SpawnProgressBridge

    assert SpawnProgressBridge._format_active_meetings_warning([]) == ""


# ─── DB notification reframing ──────────────────────────────────────


def test_create_team_notification_reframes_when_active_meeting(env_db, monkeypatch):
    """``_create_team_notification`` flips title/preview/content when the
    team is idled mid-meeting. The DB notif is what the user sees in
    /notifications — it's the user-facing version of the misleading
    "No detailed result" message.
    """
    from unittest.mock import MagicMock, patch

    from services.spawn_progress_bridge import SpawnProgressBridge

    _seed_meeting(
        env_db, meeting_id="m_stuck",
        participants=["Cameron [PM]", "Devon [BA]", "Reese [Dev]"],
        ended=False, agenda="Tools audit",
    )

    bridge = SpawnProgressBridge(progress_manager=MagicMock(), registry_db=None)

    captured_notif = {}

    class FakeNotificationModel:
        def __init__(self, **kwargs):
            captured_notif.update(kwargs)
            self.id = 99

    fake_session = MagicMock()
    fake_session.add = MagicMock()
    fake_session.commit = MagicMock()
    fake_session.refresh = MagicMock()
    fake_session.close = MagicMock()

    with patch("core.database.NotificationModel", FakeNotificationModel), \
         patch("core.database.get_db_session", return_value=fake_session), \
         patch("services.cron_scheduler.scheduler_stream_manager", MagicMock()):
        members = [
            {"agent_name": "Cameron [PM]", "status": "idle"},
            {"agent_name": "Devon [BA]", "status": "idle"},
            {"agent_name": "Reese [Dev]", "status": "idle"},
        ]
        bridge._create_team_notification(
            team_name="audit-team",
            agent_name="Cameron [PM]",
            result="",
            members=members,
        )

    # Reframed title + preview + content reflecting stalled meeting
    assert captured_notif["title"].startswith("⏳"), (
        f"Title must indicate stall, got: {captured_notif['title']}"
    )
    assert "idled" in captured_notif["title"]
    assert "intervention" in captured_notif["title"]
    assert "Cameron [PM]" in captured_notif["preview"]  # bottleneck surfaced
    assert "Active meetings detected" in captured_notif["content"]
    assert "m_stuck" in captured_notif["content"]


def test_create_team_notification_uses_default_when_no_active(env_db, monkeypatch):
    """No active meetings → original ✅ "completed" framing preserved."""
    from unittest.mock import MagicMock, patch

    from services.spawn_progress_bridge import SpawnProgressBridge

    bridge = SpawnProgressBridge(progress_manager=MagicMock(), registry_db=None)

    captured_notif = {}

    class FakeNotificationModel:
        def __init__(self, **kwargs):
            captured_notif.update(kwargs)
            self.id = 100

    fake_session = MagicMock()

    with patch("core.database.NotificationModel", FakeNotificationModel), \
         patch("core.database.get_db_session", return_value=fake_session), \
         patch("services.cron_scheduler.scheduler_stream_manager", MagicMock()):
        members = [
            {"agent_name": "PM", "status": "completed"},
            {"agent_name": "Dev", "status": "completed"},
        ]
        bridge._create_team_notification(
            team_name="solo-team",
            agent_name="PM",
            result="Built feature X.",
            members=members,
        )

    assert captured_notif["title"].startswith("✅"), (
        f"No active meetings → ✅ framing kept, got: {captured_notif['title']}"
    )
    assert "completed" in captured_notif["title"]


# ─── Fail-loud when orchestrator result is missing ──────────────────


def test_create_team_notification_fails_loud_when_result_empty(env_db, caplog):
    """Per project policy (fail loud, no silent fallbacks): when the
    orchestrator's spawn_registry.result is empty AND there is no
    active meeting to reframe against, the notification body MUST
    surface the bug — not a generic "Team has completed the work."

    This is the exact incident captured in the 2026-05-13 self-audit
    run: PM Morgan produced a roll-up but spawn_registry.result was
    never mirrored, and the user saw "No detailed result from
    orchestrator." in /notifications. The new contract surfaces
    *which* agent / team is missing data so the user knows to dig.
    """
    from unittest.mock import MagicMock, patch

    from services.spawn_progress_bridge import SpawnProgressBridge

    bridge = SpawnProgressBridge(progress_manager=MagicMock(), registry_db=None)

    captured_notif = {}

    class FakeNotificationModel:
        def __init__(self, **kwargs):
            captured_notif.update(kwargs)
            self.id = 101

    fake_session = MagicMock()

    with patch("core.database.NotificationModel", FakeNotificationModel), \
            patch("core.database.get_db_session", return_value=fake_session), \
            patch("services.cron_scheduler.scheduler_stream_manager", MagicMock()), \
            caplog.at_level("ERROR"):
        members = [
            {"agent_name": "Morgan [PM]", "status": "idle"},
            {"agent_name": "Ryan [BA]", "status": "idle"},
        ]
        bridge._create_team_notification(
            team_name="toolset-self-audit",
            agent_name="Morgan [PM]",
            result="",  # ← the bug: registry never got the roll-up
            members=members,
        )

    # ERROR log surfaces the missing-result fault so ops can grep.
    assert any(
        "Orchestrator result MISSING" in r.message
        and "toolset-self-audit" in r.message
        for r in caplog.records
    ), "fail-loud ERROR log not emitted"

    # No generic placeholder body — it must name the team + agent.
    assert "No detailed result" not in captured_notif["content"], (
        "Old silent fallback string leaked back into the notification body"
    )
    assert "BUG" in captured_notif["preview"]
    assert "Morgan [PM]" in captured_notif["preview"]
    assert "Morgan [PM]" in captured_notif["content"]
    assert "toolset-self-audit" in captured_notif["content"]
    # Body must point readers at the root-cause hook so the next reader
    # can fix it without re-discovering the design.
    assert "save_agent_context" in captured_notif["content"]
    assert "agent_context_snapshots" in captured_notif["content"]


def test_create_team_notification_writes_session_id_into_metadata(env_db):
    """The notification metadata MUST carry ``session_id`` so the
    per-session dedupe (introduced 2026-05-14) can match the right
    row. Without it a re-spawned team name shares the dedupe key
    of yesterday's run and the new completion is swallowed silently."""
    from unittest.mock import MagicMock, patch

    from services.spawn_progress_bridge import SpawnProgressBridge

    bridge = SpawnProgressBridge(progress_manager=MagicMock(), registry_db=None)
    captured: dict = {}

    class _Notif:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.id = 200

    with patch("core.database.NotificationModel", _Notif), \
            patch("core.database.get_db_session", return_value=MagicMock()), \
            patch("services.cron_scheduler.scheduler_stream_manager", MagicMock()):
        bridge._create_team_notification(
            team_name="toolset-self-audit",
            agent_name="Bailey [PM]",
            result="# Roll-up\nVerdict: PASS",
            members=[{"agent_name": "Bailey [PM]", "status": "idle"}],
            session_id="newsess",
        )

    meta = json.loads(captured["metadata_json"])
    assert meta["session_id"] == "newsess", (
        "session_id missing from notification metadata — per-session "
        "dedupe can't work without it"
    )
    assert meta["team_name"] == "toolset-self-audit"


def test_check_team_completion_filters_members_by_session_id(env_db, monkeypatch):
    """REGRESSION: cycle handler MUST scope the member sweep to the
    session_id of the triggering agent. ``team_name`` is reused across
    spawns ("toolset-self-audit" yesterday and today produce rows with
    the same name); without session filtering, old idle members from
    yesterday would contaminate today's cycle calculation.

    Pre-event-stream (2026-05-14 era) the bridge looked at all members
    of the team name regardless of session and dedupe-skipped today's
    run. Post-event-stream the handler still must filter by session;
    this test pins that contract.
    """
    from unittest.mock import MagicMock, patch

    from services.spawn_progress_bridge import SpawnProgressBridge

    fake_registry = MagicMock()
    fake_registry.get_record.return_value = {
        "run_id": "run-pm-new", "team_name": "toolset-self-audit",
        "session_id": "newsess",
    }
    fake_registry.find_by_team_name.return_value = [
        # Today's spawn — also includes 1 running worker so the worker
        # cycle is OPEN at the moment of the test snapshot. The handler
        # should NOT fire the cycle-close event because worker is still
        # running (so dedupe across sessions never gets a chance to
        # silence anything — we only need to assert session scoping).
        {"run_id": "run-pm-new", "agent_name": "Bailey [PM]", "role": "pm",
         "status": "idle", "session_id": "newsess",
         "result": "# Roll-up — Verdict: PARTIAL"},
        {"run_id": "run-toby-new", "agent_name": "Toby [BA]", "role": "ba",
         "status": "running", "session_id": "newsess"},
        # Yesterday's idle members — must NOT be counted in today's
        # session's snapshot. If they leak in, the cycle handler would
        # see "all idle" because yesterday's workers are stale-idle,
        # and emit an incorrect close event.
        {"run_id": "run-pm-old", "agent_name": "Morgan [PM]", "role": "pm",
         "status": "idle", "session_id": "oldsess", "result": ""},
        {"run_id": "run-toby-old", "agent_name": "Toby [BA]", "role": "ba",
         "status": "idle", "session_id": "oldsess"},
    ]

    bridge = SpawnProgressBridge(
        progress_manager=MagicMock(), registry_db=fake_registry,
    )
    monkeypatch.setattr(
        bridge, "_find_orchestrator",
        lambda members, session_id="": next(
            (m for m in members if m.get("role") == "pm"), None,
        ),
    )
    # Stub side-effect helpers so we can probe cycle state save.
    saved = {}
    monkeypatch.setattr(
        bridge, "_save_cycle_state",
        lambda sid, w, t, orch: saved.update(
            {"sid": sid, "w": w, "t": t, "orch": orch},
        ),
    )
    monkeypatch.setattr(
        bridge, "_load_cycle_state",
        lambda sid: {
            "worker_open": False, "team_open": False, "orch_running": False,
        },
    )

    bridge._on_member_state_event("run-pm-new")

    # Saved state reflects ONLY today's session (newsess), with worker
    # cycle still OPEN (Toby [BA] running). If yesterday's members had
    # leaked in, worker_open would still be False (their stale "idle")
    # — but worker_open=True confirms today's running member was seen.
    assert saved["sid"] == "newsess"
    assert saved["w"] is True, (
        "Session scoping broken: worker_open=False suggests yesterday's "
        "all-idle members were counted instead of today's running worker."
    )


def test_create_team_notification_keeps_full_result_when_present(env_db):
    """Sanity check the happy path — when spawn_registry.result has
    the roll-up text, that text becomes the notification body verbatim
    (no truncation, no error wrapping)."""
    from unittest.mock import MagicMock, patch

    from services.spawn_progress_bridge import SpawnProgressBridge

    bridge = SpawnProgressBridge(progress_manager=MagicMock(), registry_db=None)

    captured_notif = {}

    class FakeNotificationModel:
        def __init__(self, **kwargs):
            captured_notif.update(kwargs)
            self.id = 102

    fake_session = MagicMock()

    rollup = "# Self-audit — Team Roll-up\n\nVerdict: PARTIAL\n\n(per-member reports...)"

    with patch("core.database.NotificationModel", FakeNotificationModel), \
            patch("core.database.get_db_session", return_value=fake_session), \
            patch("services.cron_scheduler.scheduler_stream_manager", MagicMock()):
        bridge._create_team_notification(
            team_name="toolset-self-audit",
            agent_name="Morgan [PM]",
            result=rollup,
            members=[{"agent_name": "Morgan [PM]", "status": "idle"}],
        )

    assert captured_notif["content"] == rollup
    assert "BUG" not in captured_notif["preview"]
    assert "Verdict" in captured_notif["preview"]  # truncated preview shows real text


# ─── Event-stream cycle handler regression tests ────────────────────
#
# These tests replace the previous dedup-based pair
# (_check_team_completion + _notify_orchestrator_on_members_idle) which
# silently silenced repeat notifications via in-memory hash / DB content
# filter. Production incident 2026-05-16: session ``be885ae8`` got 1
# user notification on May 15 and zero thereafter despite 7+ work cycles.
#
# The 14 edge-case rows from the design table are covered by the tests
# below (E1, E2, E5, E9, E12, E13, E14 directly tested; E3/E4/E8/E10/E11
# implicit in the state-derivation logic; E7 documented out-of-scope).


def _make_bridge_with_registry(members, monkeypatch, env_db, team_name="agile"):
    """Test helper: build a bridge whose registry returns ``members``
    and whose cycle-state tables point at a fresh temp SQLite.

    Each member dict is auto-tagged with ``team_name`` so the handler's
    "is part of a team?" guard passes.
    """
    from unittest.mock import MagicMock

    from services.spawn_progress_bridge import SpawnProgressBridge

    for m in members:
        m.setdefault("team_name", team_name)

    fake_registry = MagicMock()
    fake_registry.find_by_team_name.return_value = members
    fake_registry.get_record.side_effect = lambda rid: next(
        (m for m in members if m.get("run_id") == rid), None,
    )

    bridge = SpawnProgressBridge(
        progress_manager=MagicMock(), registry_db=fake_registry,
    )
    # Default _find_orchestrator to role-based pick. Accept the new
    # ``session_id`` kwarg (2026-05-19 refactor — production lookup
    # reads ``template.orchestrator`` from ``team_sessions``; in the
    # bridge unit tests we pin the role directly).
    monkeypatch.setattr(
        bridge, "_find_orchestrator",
        lambda mems, session_id="": next(
            (m for m in mems if m.get("role") == "pm"), None,
        ),
    )
    return bridge


def test_cycle_worker_closed_fires_on_workers_idle_transition(env_db, monkeypatch):
    """**Case 1** — Worker cycle closes when all workers stop running.

    PM is still running (delegating). One worker was running, now idle.
    Worker cycle transitions open→closed → emit worker_cycle_closed.
    PM receives inbox notification.
    """
    members_running = [
        {"run_id": "pm1", "agent_name": "PM", "role": "pm",
         "status": "running", "session_id": "sX",
         "original_config": {"env_vars": {"TEAM_MESSAGES_DIR": "/tmp/x"}}},
        {"run_id": "w1", "agent_name": "Wkr", "role": "dev",
         "status": "running", "session_id": "sX",
         "original_config": {"env_vars": {"TEAM_MESSAGES_DIR": "/tmp/x"}}},
    ]
    bridge = _make_bridge_with_registry(members_running, monkeypatch, env_db)
    bridge._registry_db.get_record.return_value = {
        "run_id": "w1", "team_name": "agile", "session_id": "sX",
    }

    # First event: worker running → opens worker cycle (and team cycle);
    # the orchestrator (PM) is also running → orch_running=True.
    bridge._on_member_state_event("w1")
    state = bridge._load_cycle_state("sX")
    assert state == {
        "worker_open": True, "team_open": True, "orch_running": True,
    }

    # Now worker idle → workers all non-running → close fires
    members_running[1]["status"] = "idle"
    bridge._registry_db.get_record.return_value = members_running[1]

    emit_called = {"worker": False, "full": False}
    monkeypatch.setattr(
        bridge, "_emit_worker_cycle_closed",
        lambda *a, **kw: emit_called.update({"worker": True}),
    )
    monkeypatch.setattr(
        bridge, "_emit_full_cycle_closed",
        lambda *a, **kw: emit_called.update({"full": True}),
    )

    bridge._on_member_state_event("w1")
    assert emit_called["worker"] is True, "Worker cycle close MUST fire"
    assert emit_called["full"] is False, "Team cycle still open (PM running) — must NOT fire full"


def test_cycle_full_closed_fires_when_team_idle_no_meeting(env_db, monkeypatch):
    """**Case 2** — Team cycle closes when ALL agents (incl PM) stop
    running and no active meeting blocks. User UI notification fires.
    """
    members = [
        {"run_id": "pm1", "agent_name": "PM", "role": "pm",
         "status": "running", "session_id": "sY"},
        {"run_id": "w1", "agent_name": "Wkr", "role": "dev",
         "status": "idle", "session_id": "sY"},
    ]
    bridge = _make_bridge_with_registry(members, monkeypatch, env_db)
    monkeypatch.setattr(bridge, "_active_meetings_with_members", lambda names: [])

    # Open team cycle (orchestrator running). orch_running=True is the
    # transition we now gate on (2026-05-19 refactor).
    bridge._registry_db.get_record.return_value = members[0]
    bridge._on_member_state_event("pm1")
    state = bridge._load_cycle_state("sY")
    assert state["orch_running"] is True
    assert state["team_open"] is True

    # PM goes idle → orch_running flips True→False, no workers running →
    # full cycle closes.
    members[0]["status"] = "idle"
    bridge._registry_db.get_record.return_value = members[0]

    full_emitted = {"flag": False}
    monkeypatch.setattr(
        bridge, "_emit_full_cycle_closed",
        lambda *a, **kw: full_emitted.update({"flag": True}),
    )
    monkeypatch.setattr(bridge, "_emit_worker_cycle_closed", lambda *a, **kw: None)

    bridge._on_member_state_event("pm1")
    assert full_emitted["flag"] is True


def test_cycle_full_blocked_by_active_meeting(env_db, monkeypatch):
    """**E5** — When all agents idle BUT an active meeting exists, full
    cycle close MUST be deferred. Meeting end event will re-trigger.
    Prevents the b61af7db "All finished | No output" false-positive.
    """
    members = [
        {"run_id": "pm1", "agent_name": "PM", "role": "pm",
         "status": "running", "session_id": "sM"},
        {"run_id": "w1", "agent_name": "Wkr", "role": "dev",
         "status": "idle", "session_id": "sM"},
    ]
    bridge = _make_bridge_with_registry(members, monkeypatch, env_db)
    monkeypatch.setattr(
        bridge, "_active_meetings_with_members",
        lambda names: [{"meeting_id": "m1", "current_speaker": "Wkr"}],
    )

    bridge._registry_db.get_record.return_value = members[0]
    bridge._on_member_state_event("pm1")

    members[0]["status"] = "idle"
    bridge._registry_db.get_record.return_value = members[0]

    full_called = {"flag": False}
    monkeypatch.setattr(
        bridge, "_emit_full_cycle_closed",
        lambda *a, **kw: full_called.update({"flag": True}),
    )
    monkeypatch.setattr(bridge, "_emit_worker_cycle_closed", lambda *a, **kw: None)

    bridge._on_member_state_event("pm1")
    assert full_called["flag"] is False, (
        "Active meeting should defer full_cycle_closed — team is NOT done"
    )


def test_cycle_state_persists_across_handler_invocations(env_db, monkeypatch):
    """**E13** — Cycle state is DB-persisted so backend restart does not
    cause spurious fires or missed closes.
    """
    members = [
        {"run_id": "pm1", "agent_name": "PM", "role": "pm",
         "status": "running", "session_id": "sP"},
        {"run_id": "w1", "agent_name": "Wkr", "role": "dev",
         "status": "running", "session_id": "sP"},
    ]
    bridge = _make_bridge_with_registry(members, monkeypatch, env_db)
    bridge._registry_db.get_record.return_value = members[1]

    bridge._on_member_state_event("w1")
    state_after_open = bridge._load_cycle_state("sP")
    assert state_after_open == {
        "worker_open": True, "team_open": True, "orch_running": True,
    }

    # Simulate "restart" — recreate bridge with same registry
    from unittest.mock import MagicMock
    from services.spawn_progress_bridge import SpawnProgressBridge
    fresh_bridge = SpawnProgressBridge(
        progress_manager=MagicMock(), registry_db=bridge._registry_db,
    )
    # State must be loaded from DB, not from in-memory reset
    fresh_state = fresh_bridge._load_cycle_state("sP")
    assert fresh_state == {
        "worker_open": True, "team_open": True, "orch_running": True,
    }, (
        "Cycle state lost across restart — restart-safety contract broken"
    )


def test_cycle_event_idempotent_via_db_primary_key(env_db, monkeypatch):
    """**E12** — Same event_id inserted twice MUST be a no-op the second
    time (DB ON CONFLICT). Caller must NOT run side effects on conflict.
    """
    members = [
        {"run_id": "pm1", "agent_name": "PM", "role": "pm",
         "status": "idle", "session_id": "sI"},
    ]
    bridge = _make_bridge_with_registry(members, monkeypatch, env_db)

    payload = {"foo": "bar"}
    ok_first = bridge._insert_event(
        "stable-id-1", "worker_cycle_closed", "sI", "team", payload,
    )
    ok_second = bridge._insert_event(
        "stable-id-1", "worker_cycle_closed", "sI", "team", payload,
    )
    assert ok_first is True, "First insert should succeed"
    assert ok_second is False, (
        "Second insert with same event_id MUST conflict and return False — "
        "caller relies on this to skip duplicate side effects"
    )


def test_cycle_multi_session_isolation(env_db, monkeypatch):
    """**E14** — Cycle state is keyed by session_id. Two teams running
    concurrently MUST NOT cross-contaminate.
    """
    members = [
        {"run_id": "pm-A", "agent_name": "PM-A", "role": "pm",
         "status": "running", "session_id": "sA"},
        {"run_id": "pm-B", "agent_name": "PM-B", "role": "pm",
         "status": "idle", "session_id": "sB"},
    ]
    bridge = _make_bridge_with_registry(members, monkeypatch, env_db)

    bridge._registry_db.find_by_team_name.side_effect = (
        lambda name: [m for m in members if m.get("session_id") == (
            "sA" if name == "team-A" else "sB"
        )]
    )
    bridge._registry_db.get_record.side_effect = lambda rid: next(
        (m | {"team_name": "team-A" if m["session_id"] == "sA" else "team-B"}
         for m in members if m.get("run_id") == rid), None,
    )

    bridge._on_member_state_event("pm-A")
    bridge._on_member_state_event("pm-B")
    state_A = bridge._load_cycle_state("sA")
    state_B = bridge._load_cycle_state("sB")
    assert state_A == {
        "worker_open": False, "team_open": True, "orch_running": True,
    }
    assert state_B == {
        "worker_open": False, "team_open": False, "orch_running": False,
    }, "Session B (orch idle) leaked Session A's running state"


def test_cycle_solo_pm_no_workers(env_db, monkeypatch):
    """**E1** — PM responds to a user inject solo (no workers spawned).
    worker_cycle never opens. team_cycle open→close on PM idle fires
    full notification; no worker notification fires.
    """
    members = [
        {"run_id": "pm1", "agent_name": "PM", "role": "pm",
         "status": "running", "session_id": "sSolo"},
    ]
    bridge = _make_bridge_with_registry(members, monkeypatch, env_db)
    monkeypatch.setattr(bridge, "_active_meetings_with_members", lambda names: [])

    bridge._registry_db.get_record.return_value = members[0]
    bridge._on_member_state_event("pm1")
    state = bridge._load_cycle_state("sSolo")
    assert state == {
        "worker_open": False, "team_open": True, "orch_running": True,
    }, (
        "Solo orchestrator running: no workers → worker_open=False; "
        "orchestrator running → team_open=True, orch_running=True"
    )

    # Orchestrator idle → only full cycle fires, worker cycle never
    # opened/closed. New gating is on orch_running transition.
    members[0]["status"] = "idle"
    bridge._registry_db.get_record.return_value = members[0]

    fires = {"worker": False, "full": False}
    monkeypatch.setattr(
        bridge, "_emit_worker_cycle_closed",
        lambda *a, **kw: fires.update({"worker": True}),
    )
    monkeypatch.setattr(
        bridge, "_emit_full_cycle_closed",
        lambda *a, **kw: fires.update({"full": True}),
    )
    bridge._on_member_state_event("pm1")
    assert fires == {"worker": False, "full": True}


def test_cycle_multiround_worker_closes_each_round(env_db, monkeypatch):
    """**E2** — Multi-round delegation: PM delegates → workers idle →
    PM delegates again → workers idle. Worker cycle MUST close on EACH
    round (idempotency via unique event_id, not blocked by state-hash
    matching across rounds).
    """
    members = [
        {"run_id": "pm1", "agent_name": "PM", "role": "pm",
         "status": "running", "session_id": "sR"},
        {"run_id": "w1", "agent_name": "Wkr", "role": "dev",
         "status": "running", "session_id": "sR"},
    ]
    bridge = _make_bridge_with_registry(members, monkeypatch, env_db)
    bridge._registry_db.get_record.return_value = members[1]

    fires = {"count": 0}
    monkeypatch.setattr(
        bridge, "_emit_worker_cycle_closed",
        lambda *a, **kw: fires.update({"count": fires["count"] + 1}),
    )
    monkeypatch.setattr(bridge, "_emit_full_cycle_closed", lambda *a, **kw: None)

    # Round 1: worker running → idle
    bridge._on_member_state_event("w1")  # opens
    members[1]["status"] = "idle"
    bridge._on_member_state_event("w1")  # closes → fire 1
    assert fires["count"] == 1

    # Round 2: worker re-runs → idle again
    members[1]["status"] = "running"
    bridge._on_member_state_event("w1")  # re-opens
    members[1]["status"] = "idle"
    bridge._on_member_state_event("w1")  # closes → fire 2
    assert fires["count"] == 2, (
        "Worker cycle MUST fire each round. State-hash dedupe would "
        "incorrectly silence round 2 because (run_id, status) is identical."
    )


# ── Regression: dedupe by agent_name across stale run_ids ────────────
#
# Tracks the 2026-05-17 notif #28 incident: PM with 10 historical resume
# rows + 6 stale worker rows produced "16 agents completed" and fired
# while PM's latest run was still mid-turn. The bridge MUST collapse all
# run_ids of the same agent_name down to the latest row.


def test_cycle_dedupe_keeps_latest_run_per_agent(env_db, monkeypatch):
    """When an agent has multiple registry rows (1 per resume), only the
    LATEST row should be considered for cycle status + count.
    """
    members = [
        # 3 historical PM rows — all idle, oldest first
        {"run_id": "pm_old1", "agent_name": "PM", "role": "pm",
         "status": "idle", "session_id": "sD", "started_at": 100.0},
        {"run_id": "pm_old2", "agent_name": "PM", "role": "pm",
         "status": "idle", "session_id": "sD", "started_at": 200.0},
        # Latest PM — currently RUNNING (this is the truth)
        {"run_id": "pm_latest", "agent_name": "PM", "role": "pm",
         "status": "running", "session_id": "sD", "started_at": 999.0},
        # Worker — single row, idle
        {"run_id": "w_old", "agent_name": "Wkr", "role": "dev",
         "status": "idle", "session_id": "sD", "started_at": 150.0},
    ]
    bridge = _make_bridge_with_registry(members, monkeypatch, env_db)
    bridge._registry_db.get_record.return_value = members[2]  # latest PM

    fires = {"worker": False, "full": False}
    monkeypatch.setattr(
        bridge, "_emit_worker_cycle_closed",
        lambda *a, **kw: fires.update({"worker": True}),
    )
    monkeypatch.setattr(
        bridge, "_emit_full_cycle_closed",
        lambda *a, **kw: fires.update({"full": True}),
    )

    # Open cycle first (so prev[orch_running] is True for the next
    # call). New 4-arg signature includes orch_running.
    bridge._save_cycle_state(
        "sD", worker_open=False, team_open=True, orch_running=True,
    )
    bridge._on_member_state_event("pm_latest")

    # Latest orchestrator is running → orch_running MUST stay True →
    # no full fire. Stale idle rows must NOT dominate the latest row.
    state = bridge._load_cycle_state("sD")
    assert state["orch_running"] is True, (
        "Latest orchestrator row is running — bridge MUST treat it as "
        "running. Stale idle rows must NOT dominate the latest row's "
        "status."
    )
    assert fires["full"] is False, (
        "Premature full_cycle_closed would mean dedupe is missing — "
        "the stale idle rows are masking the real running row."
    )


def test_cycle_dedupe_count_collapses_stale_rows(env_db, monkeypatch):
    """``_emit_full_cycle_closed`` MUST receive a member list with N unique
    agents, not N*resume_count rows. Otherwise notif title shows
    "16 agents completed" instead of the real team size.
    """
    captured: dict = {}
    members = [
        # 3 stale PM rows (different run_ids, same agent) + 1 latest idle
        {"run_id": "pm_a", "agent_name": "PM", "role": "pm",
         "status": "idle", "session_id": "sE", "started_at": 100.0},
        {"run_id": "pm_b", "agent_name": "PM", "role": "pm",
         "status": "idle", "session_id": "sE", "started_at": 200.0},
        {"run_id": "pm_c", "agent_name": "PM", "role": "pm",
         "status": "idle", "session_id": "sE", "started_at": 300.0},
        # Single worker, idle
        {"run_id": "w1", "agent_name": "Wkr", "role": "dev",
         "status": "idle", "session_id": "sE", "started_at": 250.0},
    ]
    bridge = _make_bridge_with_registry(members, monkeypatch, env_db)
    bridge._registry_db.get_record.return_value = members[2]
    monkeypatch.setattr(
        bridge, "_active_meetings_with_members", lambda names: False,
    )
    monkeypatch.setattr(
        bridge, "_emit_full_cycle_closed",
        lambda sid, tname, members_list, orch, **kw: captured.setdefault(
            "members", members_list,
        ),
    )

    # Pretend orchestrator was running then idled. The new state
    # machine requires orch_running=True in the prev snapshot so the
    # transition T→F triggers a close.
    bridge._save_cycle_state(
        "sE", worker_open=False, team_open=True, orch_running=True,
    )
    bridge._on_member_state_event("pm_c")

    captured_members = captured.get("members") or []
    names = sorted(m.get("agent_name") for m in captured_members)
    assert names == ["PM", "Wkr"], (
        f"Expected 2 unique agents, got {len(captured_members)} rows: {names}. "
        "Stale resume rows must be deduped before emitting."
    )


def test_cycle_dedupe_skips_rows_with_empty_agent_name(env_db, monkeypatch):
    """Rows where ``agent_name`` is missing or empty must be filtered out,
    not crash the dedupe (defensive against corrupted/legacy registry data).
    """
    members = [
        {"run_id": "pm1", "agent_name": "PM", "role": "pm",
         "status": "running", "session_id": "sF", "started_at": 100.0},
        {"run_id": "ghost", "agent_name": "", "role": "",
         "status": "idle", "session_id": "sF", "started_at": 50.0},
    ]
    bridge = _make_bridge_with_registry(members, monkeypatch, env_db)
    bridge._registry_db.get_record.return_value = members[0]

    # Must not crash, must treat as 1-member team.
    bridge._on_member_state_event("pm1")
    state = bridge._load_cycle_state("sF")
    assert state["team_open"] is True
