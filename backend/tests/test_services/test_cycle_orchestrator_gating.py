"""Regression tests for the orchestrator-gated cycle close semantics.

This file pins the 2026-05-19 fix for the duplicate-notify incident.

## The incident (one-paragraph history)

User session ``be885ae8`` produced 2 ``team_completion`` notifications
32 seconds apart:

* ``notif #39`` at 16:41:40 — fired the instant the last worker idled,
  even though the orchestrator (Robin [PM]) had not yet had its turn.
  ``team_open = any(spawned running)`` transitioned True→False because
  the orchestrator was *already* idle (waiting). This was a false
  close — the system pushed a worker-status report into the orch's
  inbox and was about to wake it up.

* ``notif #40`` at 16:42:12 — the *real* close, fired after the orch
  was woken, read the worker report, produced its own outcome, and
  idled again.

The user saw 2 nearly-identical "team done" notifications and asked
why. Root cause: ``team_open = any(running)`` is the wrong gating
signal — it fires when *any* member finishes, not when the
orchestrator finishes its turn. The fix is to gate ``full_cycle_closed``
on the orchestrator's OWN transition ``orch_running: True → False``,
treating worker-idle-with-orch-already-idle as a NO-OP for the
user-facing notify.

## What these tests pin

1. ``orchestrator already idle + worker idles`` → MUST NOT emit
   ``full_cycle_closed``. (the bogus notif #39 case)
2. ``orchestrator running → idle (workers idle)`` → MUST emit exactly
   one ``full_cycle_closed``. (the legitimate notif #40 case)
3. Concurrent emit attempts within one cycle close MUST produce
   exactly one DB row (atomic ``UPDATE … RETURNING WHERE orch_running=1``).
4. reopen → close → reopen → close MUST produce 2 distinct
   ``team_close_seq`` values and 2 distinct ``team_events`` rows.
5. Schema migration is idempotent (multiple ``_ensure_event_tables``
   calls don't crash on duplicate-column errors).
6. ``_find_orchestrator`` honours ``template.orchestrator`` from
   ``team_sessions`` — not substring matching on the role name.

The tests use real SQLite (via ``SPAWN_REGISTRY_DB`` env var pointing
at a temp file) so the atomic UPDATE…RETURNING semantics are exercised
against the actual database engine — not a mock. This is the
integration-vs-unit distinction from CLAUDE.md item 4: a mock-only
test of "atomic update" is worse than no test.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point ``SPAWN_REGISTRY_DB`` at a fresh temp file.

    Returns the DB path. The bridge will auto-create ``team_events``
    and ``team_cycle_state`` on first use via ``_ensure_event_tables``.
    """
    db_path = tmp_path / "jarvis.db"
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(db_path))
    return db_path


@pytest.fixture
def fake_registry():
    """Build a ``registry_db`` MagicMock with controllable members.

    The ``set_members`` method on the returned mock lets each test
    swap in a new member list and recompute ``get_record`` /
    ``find_by_team_name`` answers in one place.
    """
    reg = MagicMock()
    state = {"members": []}

    def set_members(members):
        state["members"] = members
        reg.find_by_team_name.side_effect = lambda name: [
            m for m in state["members"] if m.get("team_name") == name
        ]
        reg.get_record.side_effect = lambda rid: next(
            (m for m in state["members"] if m.get("run_id") == rid),
            None,
        )

    reg.set_members = set_members  # type: ignore[attr-defined]
    set_members([])
    return reg


@pytest.fixture
def bridge(fake_registry, temp_db, monkeypatch):
    """Construct a bridge wired to the fake registry + temp DB.

    Stubs out:
      * ``_active_meetings_with_members`` — returns [] (no meeting
        gating in these tests; the meeting-aware path has its own
        test file).
      * ``_create_team_notification`` — replaced with a probe so we
        can count user-facing notifies without hitting the SQLAlchemy
        ``NotificationModel``.
      * ``_resolve_messages_dir`` — returns a temp dir so the worker
        report path doesn't blow up trying to write a real file.
    """
    from services.spawn_progress_bridge import SpawnProgressBridge

    b = SpawnProgressBridge(
        progress_manager=MagicMock(),
        registry_db=fake_registry,
    )

    monkeypatch.setattr(b, "_active_meetings_with_members", lambda names: [])

    probes = {"team_notify": [], "worker_notify": []}

    def _capture_team_notify(team_name, agent_name, result, members, *,
                             session_id=""):
        probes["team_notify"].append({
            "team_name": team_name,
            "agent_name": agent_name,
            "session_id": session_id,
            "member_count": len(members),
        })

    monkeypatch.setattr(b, "_create_team_notification", _capture_team_notify)

    # The real worker notify path tries MessageBus + asyncio loop; for
    # transition-counting tests we just want to know it was called.
    real_emit_worker = b._emit_worker_cycle_closed

    def _wrap_emit_worker(*args, **kwargs):
        probes["worker_notify"].append({
            "args": args,
            "close_seq": kwargs.get("close_seq"),
        })
        # Don't delegate — the real impl does MessageBus IO. We just
        # need to confirm it was called with the right seq.

    monkeypatch.setattr(b, "_emit_worker_cycle_closed", _wrap_emit_worker)

    b._probes = probes  # type: ignore[attr-defined]
    return b


def _member(
    run_id: str,
    *,
    role: str,
    status: str,
    session_id: str,
    team_name: str = "team-A",
    agent_name: str | None = None,
    started_at: float | None = None,
    result: str = "",
) -> dict:
    """Build a registry-row shaped dict for the tests."""
    return {
        "run_id": run_id,
        "agent_name": agent_name or f"{role}-{run_id}",
        "role": role,
        "status": status,
        "session_id": session_id,
        "team_name": team_name,
        "started_at": started_at if started_at is not None else time.time(),
        "result": result,
    }


def _seed_team_session(
    db_path: Path,
    session_id: str,
    *,
    team_name: str,
    orchestrator_role: str,
) -> None:
    """Insert a minimal ``team_sessions`` row so the bridge's
    ``_lookup_orchestrator_role`` returns the expected role.

    The schema (``session_id TEXT PRIMARY KEY, data_json TEXT NOT NULL``)
    mirrors the production table created by ``core.agent_registry_db``.
    """
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS team_sessions (
                session_id TEXT PRIMARY KEY,
                data_json  TEXT NOT NULL
            )"""
        )
        conn.execute(
            "INSERT OR REPLACE INTO team_sessions(session_id, data_json) VALUES(?, ?)",
            (
                session_id,
                json.dumps({
                    "session_id": session_id,
                    "team_name": team_name,
                    "template": {
                        "name": team_name,
                        "orchestrator": orchestrator_role,
                        "roles": {orchestrator_role: {}, "dev": {}},
                    },
                    "agents": {},
                }),
            ),
        )
        conn.commit()


# ─── 1. Bogus notify #39 — orchestrator already idle + worker idles ───


def test_full_cycle_NOT_fired_when_orchestrator_already_idle(
    bridge, fake_registry, temp_db,
):
    """The 2026-05-19 incident replay.

    Setup: orchestrator is idle, last worker transitions running →
    idle. Old code observed ``team_open = any(running)`` flip True→False
    and fired ``full_cycle_closed`` even though the orchestrator was
    asleep waiting for the worker report it had just been pushed.
    Expected new behaviour: NO ``full_cycle_closed`` emitted.

    A ``worker_cycle_closed`` IS expected (workers stopped) — that's
    the path that wakes the orchestrator.
    """
    _seed_team_session(
        temp_db, "sess-incident", team_name="team-A", orchestrator_role="pm",
    )
    pm = _member("pm-1", role="pm", status="idle", session_id="sess-incident")
    wkr_running = _member(
        "wkr-1", role="dev", status="running", session_id="sess-incident",
    )
    fake_registry.set_members([pm, wkr_running])

    # First event opens the worker cycle.
    bridge._on_member_state_event("wkr-1")
    state = bridge._load_cycle_state("sess-incident")
    assert state["worker_open"] is True
    assert state["orch_running"] is False, (
        "Orchestrator is idle from the start — must NOT be flagged as running"
    )

    # Worker idles.
    wkr_running["status"] = "idle"
    bridge._on_member_state_event("wkr-1")

    # Worker-close fires (orch needs to be woken).
    assert len(bridge._probes["worker_notify"]) == 1, (
        f"worker_cycle_closed should fire exactly once; got "
        f"{len(bridge._probes['worker_notify'])}"
    )
    # Full-close MUST NOT fire — orchestrator hasn't had its turn yet.
    assert bridge._probes["team_notify"] == [], (
        "REGRESSION: full_cycle_closed fired even though the "
        "orchestrator was already idle and never transitioned "
        "running→idle. This is the 2026-05-19 notify #39 bug."
    )


# ─── 2. Legitimate notify — orchestrator running → idle ──────────────


def test_full_cycle_fires_when_orchestrator_transitions_to_idle(
    bridge, fake_registry, temp_db,
):
    """The notif #40 case — orchestrator finishes its turn after
    workers, and we want exactly one user-facing notify.
    """
    _seed_team_session(
        temp_db, "sess-ok", team_name="team-A", orchestrator_role="pm",
    )
    pm = _member(
        "pm-1", role="pm", status="running", session_id="sess-ok",
        result="rollup body",
    )
    wkr = _member(
        "wkr-1", role="dev", status="idle", session_id="sess-ok",
    )
    fake_registry.set_members([pm, wkr])

    # Open: orch_running=True snapshot is persisted.
    bridge._on_member_state_event("pm-1")
    assert bridge._load_cycle_state("sess-ok")["orch_running"] is True

    # Orchestrator idles.
    pm["status"] = "idle"
    bridge._on_member_state_event("pm-1")

    assert len(bridge._probes["team_notify"]) == 1, (
        f"Expected exactly 1 full_cycle_closed notify; got "
        f"{len(bridge._probes['team_notify'])}"
    )
    assert bridge._probes["team_notify"][0]["session_id"] == "sess-ok"


# ─── 3. Atomic close — concurrent callers, single notify ─────────────


def test_concurrent_close_attempts_emit_exactly_once(
    bridge, fake_registry, temp_db,
):
    """Two ``_on_member_state_event`` invocations observing the same
    transition MUST produce exactly one ``full_cycle_closed`` emit.

    Atomic-UPDATE…RETURNING ``WHERE orch_running = 1`` is the gating
    primitive: only the first caller's UPDATE matches the WHERE, so
    only one caller returns a non-None close_seq → only one emit.

    The earlier implementation embedded ``uuid.uuid4()`` in
    ``event_id``, so the DB-PK conflict dedupe in ``_insert_event``
    never engaged: 2 concurrent callers minted 2 different event_ids
    → 2 emits → 2 user notifications.
    """
    _seed_team_session(
        temp_db, "sess-race", team_name="team-A", orchestrator_role="pm",
    )
    pm = _member(
        "pm-1", role="pm", status="running", session_id="sess-race",
        result="x",
    )
    fake_registry.set_members([pm])

    # Prime ``orch_running=True``.
    bridge._on_member_state_event("pm-1")
    pm["status"] = "idle"

    # Two callers race. We simulate by invoking the handler twice
    # back-to-back. The second call will see ``orch_running=0`` in
    # team_cycle_state (the first call's UPDATE already flipped it),
    # so its ``WHERE orch_running=1`` predicate misses → no close.
    bridge._on_member_state_event("pm-1")
    bridge._on_member_state_event("pm-1")

    assert len(bridge._probes["team_notify"]) == 1, (
        f"Atomic close failed — got {len(bridge._probes['team_notify'])} "
        f"notifies for one logical cycle close. The WHERE orch_running=1 "
        f"predicate must serialise concurrent callers."
    )

    # Verify the DB row has team_close_seq incremented exactly once.
    with sqlite3.connect(str(temp_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT team_close_seq FROM team_cycle_state WHERE session_id=?",
            ("sess-race",),
        ).fetchone()
    assert row is not None
    assert row["team_close_seq"] == 1, (
        f"team_close_seq={row['team_close_seq']} — should be 1 after "
        f"one logical close. >1 means the atomic guard let a second "
        f"caller through; 0 means the close never fired."
    )


# ─── 4. Multi-cycle: reopen→close→reopen→close → 2 distinct seqs ─────


def test_reopen_close_produces_distinct_close_seqs(
    bridge, fake_registry, temp_db,
):
    """Two legitimate close cycles (orchestrator running→idle, then
    running→idle again after a reopen) MUST get distinct
    ``team_close_seq`` values AND distinct ``team_events`` rows.

    This is the case the user explicitly approved: if the
    orchestrator wakes after a close (e.g. an inbox message) and
    idles again, that IS a new cycle and DOES deserve a new notify.
    """
    _seed_team_session(
        temp_db, "sess-multi", team_name="team-A", orchestrator_role="pm",
    )
    pm = _member(
        "pm-1", role="pm", status="running", session_id="sess-multi",
        result="cycle 1",
    )
    fake_registry.set_members([pm])

    bridge._on_member_state_event("pm-1")  # open
    pm["status"] = "idle"
    bridge._on_member_state_event("pm-1")  # close 1

    pm["status"] = "running"
    bridge._on_member_state_event("pm-1")  # reopen
    pm["status"] = "idle"
    bridge._on_member_state_event("pm-1")  # close 2

    assert len(bridge._probes["team_notify"]) == 2, (
        f"Expected 2 notifies across 2 cycles; got "
        f"{len(bridge._probes['team_notify'])}"
    )

    # team_events should hold 2 distinct full_cycle_closed rows.
    with sqlite3.connect(str(temp_db)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT event_id FROM team_events
               WHERE session_id=? AND event_type='full_cycle_closed'
               ORDER BY created_at""",
            ("sess-multi",),
        ).fetchall()
    assert [r["event_id"] for r in rows] == [
        "sess-multi:full_cycle_closed:1",
        "sess-multi:full_cycle_closed:2",
    ], (
        f"event_ids should be stable seq-based (not uuid-based): {rows}"
    )


# ─── 5. Schema migration idempotency ─────────────────────────────────


def test_ensure_event_tables_is_idempotent(bridge, temp_db):
    """Calling ``_ensure_event_tables`` multiple times must NOT crash
    on duplicate-column errors. The bridge calls it on every event,
    so any per-event failure would manifest as a flood of warning
    logs in production.
    """
    # First call already happened during __init__. Call 5 more times.
    for _ in range(5):
        bridge._ensure_event_tables()

    # Verify the migration columns are present exactly once.
    with sqlite3.connect(str(temp_db)) as conn:
        conn.row_factory = sqlite3.Row
        cols = [r["name"] for r in conn.execute(
            "PRAGMA table_info(team_cycle_state)"
        ).fetchall()]
    assert cols.count("orch_running") == 1
    assert cols.count("team_close_seq") == 1
    assert cols.count("worker_close_seq") == 1


# ─── 6. _find_orchestrator honours template.orchestrator ─────────────


def test_find_orchestrator_uses_template_field_not_substring(
    bridge, temp_db, fake_registry,
):
    """Templates whose orchestrator role is NOT named "pm" still work.

    The old code matched substring ``"pm" in role.lower()``, which
    silently failed for any template that named its orchestrator
    "lead", "ba", "coordinator", etc. With the audit 2026-05-19 fix,
    the bridge reads ``team_sessions.template.orchestrator`` and
    matches the role by exact (case-insensitive) equality.
    """
    _seed_team_session(
        temp_db, "sess-custom-orch",
        team_name="team-custom", orchestrator_role="lead",
    )
    lead = _member(
        "lead-1", role="lead", status="running",
        session_id="sess-custom-orch", team_name="team-custom",
    )
    dev = _member(
        "dev-1", role="dev", status="idle",
        session_id="sess-custom-orch", team_name="team-custom",
    )
    fake_registry.set_members([lead, dev])

    found = bridge._find_orchestrator(
        [lead, dev], session_id="sess-custom-orch",
    )
    assert found is lead, (
        f"_find_orchestrator picked {found.get('role')} but the "
        f"template declares 'lead' as orchestrator. Substring 'pm' "
        f"match is gone — template.orchestrator is the single source "
        f"of truth."
    )


def test_find_orchestrator_falls_back_to_first_spawned_without_session(
    bridge, fake_registry,
):
    """Ad-hoc / legacy callers without a ``session_id`` fall back to
    ``first spawned`` — the orchestrator spawns first by construction.
    Preserves backward compatibility for the rare callers that don't
    have a session_id.
    """
    earliest = _member(
        "a-1", role="any", status="idle", session_id="",
        started_at=100.0,
    )
    later = _member(
        "b-1", role="any", status="idle", session_id="",
        started_at=200.0,
    )
    found = bridge._find_orchestrator([later, earliest])
    assert found is earliest


# ─── 7. The 2026-05-19 sequence reconstructed end-to-end ─────────────


def test_2026_05_19_incident_sequence_emits_exactly_one_notify(
    bridge, fake_registry, temp_db,
):
    """Replay the exact incident sequence (log timestamps in module
    docstring) and assert the new state machine produces 1 notify,
    not 2.

    Steps from the log:
      A. 16:41:30 — worker QE is running (cycle is open).
      B. 16:41:40 — worker QE idles. Worker cycle closes. Bridge
         wakes the orchestrator (which was already idle).
      C. 16:41:40 — orchestrator wakes (running), processes report.
      D. 16:42:12 — orchestrator idles after producing its outcome.

    Old code produced notifies at (B) and (D) → 2 notifications.
    New code produces a notify ONLY at (D).
    """
    _seed_team_session(
        temp_db, "be885ae8",
        team_name="agile-team", orchestrator_role="pm",
    )
    pm = _member(
        "pm-resume-1", role="pm", status="idle",
        session_id="be885ae8", team_name="agile-team",
    )
    qe = _member(
        "qe-1", role="qe", status="running",
        session_id="be885ae8", team_name="agile-team",
    )
    fake_registry.set_members([pm, qe])

    # A. Worker is running → opens worker cycle.
    bridge._on_member_state_event("qe-1")

    # B. QE goes idle. orch was already idle → no full close.
    qe["status"] = "idle"
    bridge._on_member_state_event("qe-1")

    # Verify no premature notify.
    assert bridge._probes["team_notify"] == [], (
        "Step B fired full_cycle_closed prematurely — the 2026-05-19 "
        "notif #39 bug has regressed."
    )

    # C. Orchestrator is woken (production: _trigger_orchestrator_resume).
    pm["status"] = "running"
    pm["result"] = "Final consolidated status: BLOCKED"
    bridge._on_member_state_event("pm-resume-1")

    # D. Orchestrator finishes its turn and idles.
    pm["status"] = "idle"
    bridge._on_member_state_event("pm-resume-1")

    assert len(bridge._probes["team_notify"]) == 1, (
        f"Expected exactly 1 notify across the incident sequence; "
        f"got {len(bridge._probes['team_notify'])}. The new state "
        f"machine must gate on orch_running transition, not "
        f"team_open."
    )
