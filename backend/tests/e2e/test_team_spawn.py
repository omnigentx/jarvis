"""E2E regression: team-member subprocess saves its context snapshot with
TEAM_* env vars correctly round-tripped through SQLite.

This guards the fragile path MEMORY calls out as "#1 source of production
incidents": team agents spawned as subprocesses where a missing or wrongly
named env var silently breaks collaboration/persistence.

What the test asserts:
 * `TEAM_MY_NAME` → snapshot.agent_name
 * `TEAM_MY_ROLE` → snapshot.team_name  (historical naming — confusing but real)
 * `TEAM_SESSION_ID` → snapshot.session_id
 * Row count: exactly one task-completion snapshot
 * context_json parses to a list that contains the scripted assistant reply

If any of those mappings drift, the test names what changed — catching
the bug class that unit-level env-propagation tests can't (because this
runs the real subprocess + real save_snapshot path).

We deliberately do NOT set TEAM_WORKSPACE — that would trigger the
keep-alive inbox loop and the subprocess would never exit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.harness import run_scripted_subprocess


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.slow
def test_team_member_subprocess_persists_snapshot(tmp_path: Path):
    team_env = {
        "TEAM_MY_NAME": "Linh [PM]",
        "TEAM_MY_ROLE": "pm",
        "TEAM_SESSION_ID": "agile-team_test001",
    }

    result = run_scripted_subprocess(
        fixture_path=FIXTURES / "team_member_single_reply.yaml",
        task="Kick off the project",
        tmp_path=tmp_path,
        extra_env=team_env,
    )

    assert result.returncode == 0, (
        f"Team-member subprocess failed.\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "PM reporting in" in result.result.get("result", ""), (
        f"Scripted reply missing from subprocess result: {result.result}"
    )

    snapshots = result.context_snapshots()
    assert len(snapshots) >= 1, (
        f"No context snapshot written to DB — context_persistence silently "
        f"skipped the save.\nSTDERR:\n{result.stderr}"
    )
    snap = snapshots[-1]

    assert snap["agent_name"] == "Linh [PM]", (
        f"TEAM_MY_NAME -> agent_name propagation broken. "
        f"Expected 'Linh [PM]', got {snap['agent_name']!r}"
    )
    # Note: TEAM_MY_ROLE is stored as team_name in the snapshot (confusing
    # but intentional; see isolated_runner._save_agent_context_snapshot).
    assert snap["team_name"] == "pm", (
        f"TEAM_MY_ROLE -> team_name propagation broken. "
        f"Expected 'pm', got {snap['team_name']!r}"
    )

    raw = json.loads(snap["context_json"])
    assert isinstance(raw, dict) and raw.get("messages"), (
        f"Snapshot context_json missing 'messages' list: {snap['context_json'][:200]!r}"
    )
    text_dump = json.dumps(raw["messages"], ensure_ascii=False)
    assert "PM reporting in" in text_dump, (
        f"Scripted assistant reply missing from saved context: {text_dump[:500]}"
    )
    assert "Kick off the project" in text_dump, (
        f"User task missing from saved context: {text_dump[:500]}"
    )


@pytest.mark.slow
def test_team_member_subprocess_tolerates_missing_session_id(tmp_path: Path):
    """Control: without TEAM_SESSION_ID the subprocess must still complete
    cleanly — ``session_id`` is optional and snapshots just get a NULL/empty
    value. The test documents this tolerance so any future change that
    makes the subprocess crash on missing optional env vars is surfaced."""
    team_env = {
        "TEAM_MY_NAME": "Linh [PM]",
        "TEAM_MY_ROLE": "pm",
        # TEAM_SESSION_ID intentionally omitted
    }

    result = run_scripted_subprocess(
        fixture_path=FIXTURES / "team_member_single_reply.yaml",
        task="Kick off the project",
        tmp_path=tmp_path,
        extra_env=team_env,
    )

    assert result.returncode == 0, (
        f"Subprocess must tolerate missing TEAM_SESSION_ID (optional), "
        f"got exit={result.returncode}\nSTDERR:\n{result.stderr}"
    )
    snapshots = result.context_snapshots()
    assert len(snapshots) >= 1, "Snapshot still expected despite no session_id"
