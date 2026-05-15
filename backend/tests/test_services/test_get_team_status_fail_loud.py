"""Fail-loud contract for ``agent_spawner__get_team_status``.

The orchestrator (and Jarvis) reads ``get_team_status`` to decide
whether to resume / wake / kill agents. The post-fix contract:

* An agent whose registry status is ``running`` but has had no
  LLM/tool activity in > 30s is reported as ``status="stuck"`` —
  that's a genuine hang mid-turn.
* ``idle`` is NEVER reclassified as stuck. Resumable agents
  legitimately go idle between turns and park there for hours
  waiting for inbox messages; flagging them as stuck makes the
  dashboard scream right after a healthy kickoff. The "Devon" case
  (turn returns idle but with no useful output) is detected on the
  READ paths (``get_team_result`` / notification) where an empty
  ``spawn_registry.result`` raises ``error_state``.
* Sprint-level ``sprint_status`` is ``"stuck"`` whenever any agent is
  stuck — never ``"completed"`` — so the orchestrator does not
  declare victory on a half-frozen team.
* ``progress`` includes a ``"N stuck"`` suffix when any agent is
  stuck, so callers reading the one-line summary still see the gap.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ─────────────────────────────────────────────────────────


def _make_session(agents: dict, *, template_name: str = "agile-team",
                  workspace="/tmp/ws"):
    session = MagicMock()
    session.agents = agents
    session.template = {"name": template_name}
    session.workspace = Path(workspace)
    return session


def _record(status: str, *, last_active_at: float | None,
            result: str = "") -> MagicMock:
    rec = MagicMock()
    rec.status = status
    rec.result = result
    rec.last_active_at = last_active_at
    return rec


def _call(session, registry_map):
    """Wire mocks, call get_team_status, parse JSON."""
    from fast_agent.spawn.servers import agent_spawner_server as srv

    fake_registry = MagicMock()
    fake_registry.get_latest.side_effect = lambda rid: registry_map.get(rid)

    with patch.object(srv, "get_team_session", return_value=session), \
            patch.object(srv, "_registry", fake_registry):
        return json.loads(srv.get_team_status("s1"))


# ── Tests ───────────────────────────────────────────────────────────


def test_idle_with_recent_activity_stays_idle():
    """Agent that went idle moments ago must NOT be flagged stuck —
    "idle" is normal for resumable agents between turns."""
    now = time.time()
    agents = {
        "Morgan [PM]": {
            "run_id": "run-pm", "role": "pm", "status": "running",
            "agent_name": "Morgan [PM]",
        },
    }
    payload = _call(
        _make_session(agents),
        {"run-pm": _record("idle", last_active_at=now - 5)},  # 5s ago
    )

    pm = payload["agents"]["Morgan [PM]"]
    assert pm["status"] == "idle"
    assert "stuck_seconds" not in pm
    assert payload["sprint_status"] == "completed"


def test_idle_is_never_reclassified_as_stuck_no_matter_how_old():
    """Resumable agents park in ``idle`` indefinitely between turns —
    a 20-minute-old idle agent is normal, not stuck. Reclassifying it
    as stuck (the 2026-05-14 over-eager first attempt) made the
    dashboard show "7 agents stuck" right after a healthy kickoff."""
    now = time.time()
    agents = {
        "Parked [BA]": {
            "run_id": "run-parked", "role": "ba", "status": "running",
            "agent_name": "Parked [BA]",
        },
    }
    payload = _call(
        _make_session(agents),
        {"run-parked": _record("idle", last_active_at=now - 1200)},  # 20 min
    )

    parked = payload["agents"]["Parked [BA]"]
    assert parked["status"] == "idle"
    assert "stuck_seconds" not in parked
    assert "raw_status" not in parked
    assert payload["sprint_status"] == "completed", (
        "all-idle team after kickoff must NOT scream 'stuck'"
    )


def test_running_with_stale_last_active_is_stuck():
    """``status="running"`` past the threshold IS a genuine hang
    (LLM call or post-tool processing got stuck). Surface it loudly
    so the orchestrator can resume / kill the agent."""
    now = time.time()
    agents = {
        "Hung [Dev]": {
            "run_id": "run-hang", "role": "dev", "status": "pending",
            "agent_name": "Hung [Dev]",
        },
    }
    payload = _call(
        _make_session(agents),
        {"run-hang": _record("running", last_active_at=now - 90)},
    )

    hung = payload["agents"]["Hung [Dev]"]
    assert hung["status"] == "stuck"
    assert hung["raw_status"] == "running"
    assert hung["stuck_seconds"] >= 89  # account for clock drift in test
    assert payload["sprint_status"] == "stuck"
    assert "1 stuck" in payload["progress"]


def test_idle_team_with_one_running_hang_surfaces_only_the_hung():
    """6 normally-idle members + 1 stuck-running member. The 6 idle
    stay idle (parked, healthy); only the running-hang flips to
    stuck. Sprint status reflects the hang."""
    now = time.time()
    agents = {}
    registry = {}
    for i, name in enumerate(["A", "B", "C", "D", "E", "F"]):
        rid = f"run-ok-{i}"
        agents[f"{name} [Member]"] = {
            "run_id": rid, "role": "member",
            "agent_name": f"{name} [Member]", "status": "running",
        }
        registry[rid] = _record(
            "idle", last_active_at=now - 5,
            result=f"Audit report from {name}",
        )
    agents["Hung [Dev]"] = {
        "run_id": "run-hang", "role": "dev",
        "agent_name": "Hung [Dev]", "status": "running",
    }
    registry["run-hang"] = _record(
        "running", last_active_at=now - 1200,  # 20 min hang
    )

    payload = _call(_make_session(agents), registry)

    assert payload["sprint_status"] == "stuck"
    assert payload["agents"]["Hung [Dev]"]["status"] == "stuck"
    assert payload["agents"]["A [Member]"]["status"] == "idle"
    assert "6/7" in payload["progress"]
    assert "1 stuck" in payload["progress"]


def test_terminal_status_never_reclassified_as_stuck():
    """``completed`` / ``error`` / ``cancelled`` are terminal — they
    have no upstream activity and must not be flagged stuck even if
    last_active_at is old."""
    now = time.time()
    agents = {
        "Done [QE]": {
            "run_id": "run-done", "role": "qe", "status": "running",
            "agent_name": "Done [QE]",
        },
        "Err [BA]": {
            "run_id": "run-err", "role": "ba", "status": "running",
            "agent_name": "Err [BA]",
        },
    }
    payload = _call(_make_session(agents), {
        "run-done": _record("completed", last_active_at=now - 9999,
                            result="done"),
        "run-err": _record("error", last_active_at=now - 9999),
    })

    assert payload["agents"]["Done [QE]"]["status"] == "completed"
    assert payload["agents"]["Err [BA]"]["status"] == "error"
    # All terminal → sprint completed (but errors flagged)
    assert payload["sprint_status"] == "completed"
    assert "1 errors" in payload["progress"]


def test_no_last_active_at_does_not_crash_and_does_not_flag_stuck():
    """Fresh-spawn race: registry row exists but ``last_active_at`` is
    None because the first turn hasn't reported activity yet. Must
    fall through to the raw status (idle/running), not crash and not
    falsely flag stuck."""
    agents = {
        "Fresh [Dev]": {
            "run_id": "run-fresh", "role": "dev", "status": "pending",
            "agent_name": "Fresh [Dev]",
        },
    }
    payload = _call(
        _make_session(agents),
        {"run-fresh": _record("running", last_active_at=None)},
    )

    fresh = payload["agents"]["Fresh [Dev]"]
    assert fresh["status"] == "running"
    assert "stuck_seconds" not in fresh


def test_progress_string_omits_stuck_suffix_when_no_one_stuck():
    """Don't pollute the progress string with ", 0 stuck" when no
    agent is stuck — the orchestrator scans this line and we want
    the absence of stuck to be visually obvious."""
    now = time.time()
    agents = {
        "Ok [Dev]": {
            "run_id": "run-ok", "role": "dev", "status": "running",
            "agent_name": "Ok [Dev]",
        },
    }
    payload = _call(
        _make_session(agents),
        {"run-ok": _record("idle", last_active_at=now - 1)},
    )

    assert "stuck" not in payload["progress"]
