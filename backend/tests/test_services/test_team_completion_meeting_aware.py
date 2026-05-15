"""Regression tests for the meeting-aware team-completion notifications.

Background (b61af7db incident, 2026-05-09): when a team idled mid-meeting
the orchestrator received two misleading "team finished" messages:

* MessageBus inbox: "📋 Team Status Update — All members have finished. |
  Reese [Dev] | ✅ idle | No output |..."
* DB notification: "✅ Team tools-audit-team hoàn thành (4 agents) — Không
  có kết quả chi tiết từ orchestrator."

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
    "Không có kết quả chi tiết" message.
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
    """No active meetings → original ✅ "hoàn thành" framing preserved."""
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
    assert "hoàn thành" in captured_notif["title"]


# ─── Fail-loud when orchestrator result is missing ──────────────────


def test_create_team_notification_fails_loud_when_result_empty(env_db, caplog):
    """Per project policy (fail loud, no silent fallbacks): when the
    orchestrator's spawn_registry.result is empty AND there is no
    active meeting to reframe against, the notification body MUST
    surface the bug — not a generic "Team đã hoàn thành công việc."

    This is the exact incident captured in the 2026-05-13 self-audit
    run: PM Morgan produced a roll-up but spawn_registry.result was
    never mirrored, and the user saw "Không có kết quả chi tiết từ
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
    assert "Không có kết quả chi tiết" not in captured_notif["content"], (
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
    """REGRESSION: ``_check_team_completion`` MUST scope the member
    sweep to the session_id of the triggering agent. The team_name
    is reused across spawns ("toolset-self-audit" yesterday and
    today both produce rows with that ``team_name``), so a raw
    ``find_by_team_name`` join mixes the old idle agents into the
    new completion check.

    Pre-2026-05-14 the bridge looked at all members of the team
    name regardless of session and dedupe-skipped today's run
    because yesterday's notification already existed under that
    name. The fix: filter members + dedupe by session_id.
    """
    from unittest.mock import MagicMock, patch

    from services.spawn_progress_bridge import SpawnProgressBridge

    fake_registry = MagicMock()
    fake_registry.get_record.return_value = {
        "run_id": "run-pm-new", "team_name": "toolset-self-audit",
        "session_id": "newsess",
    }
    # The registry returns BOTH sessions' members under the name.
    # The fix filters them down to newsess before the all-done check.
    fake_registry.find_by_team_name.return_value = [
        # Today's
        {"run_id": "run-pm-new", "agent_name": "Bailey [PM]", "role": "pm",
         "status": "idle", "session_id": "newsess",
         "result": "# Roll-up — Verdict: PARTIAL"},
        # Yesterday's — must be excluded
        {"run_id": "run-pm-old", "agent_name": "Morgan [PM]", "role": "pm",
         "status": "idle", "session_id": "oldsess", "result": ""},
    ]

    bridge = SpawnProgressBridge(
        progress_manager=MagicMock(), registry_db=fake_registry,
    )
    # Force the dedupe to miss so we can observe the creation path.
    monkeypatch.setattr(bridge, "_has_team_notification", lambda sid: False)

    captured: dict = {}

    class _Notif:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.id = 201

    with patch("core.database.NotificationModel", _Notif), \
            patch("core.database.get_db_session", return_value=MagicMock()), \
            patch("services.cron_scheduler.scheduler_stream_manager", MagicMock()):
        bridge._check_team_completion(
            trigger_agent="Bailey [PM]",
            raw={"run_id": "run-pm-new"},
        )

    # Notification must surface TODAY's roll-up, not yesterday's empty.
    assert captured, "completion check skipped — likely picked the wrong session"
    assert "Roll-up" in captured["content"]
    assert "Verdict" in captured["content"]
    # Metadata carries today's session_id so future dedupe matches it.
    meta = json.loads(captured["metadata_json"])
    assert meta["session_id"] == "newsess"
    assert meta["agent"] == "Bailey [PM]"


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
