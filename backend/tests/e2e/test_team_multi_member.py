"""E2E regression: multi-member team subprocess spawn flow.

Extends ``test_team_spawn.py`` (single member) to cover the real multi-agent
collaboration shape: PM + Dev (or multiple Dev) running as separate
subprocesses that all write context snapshots into the SAME SQLite DB under
the SAME ``session_id``.

What these tests guard (in order):

* **Shared session_id across members** — a TeamSession is defined by the
  common ``TEAM_SESSION_ID`` propagated through every spawned member.
  Regression: an env-var typo or per-member DB path silently splits one
  team into N lone-agent sessions.
* **Parallel spawn safety** — two Dev members writing snapshots
  concurrently must both land in the shared DB with distinct agent_names.
  Guards against SQLite lock/race regressions in ``save_agent_context``.
* **Env-var → snapshot contract** — TEAM_MY_NAME/TEAM_MY_ROLE/TEAM_SESSION_ID
  must round-trip to ``agent_name`` / ``team_name`` / ``session_id`` columns.

``team_communicate`` tool coverage is intentionally deferred (skipped test)
because wiring the ``team-communicate`` MCP server into the current
subprocess harness requires authoring a full parent fastagent.config.yaml
with the server spawn spec — out of scope for these tests. The MessageBus
file-contract is already covered at unit-test level in the fast-agent fork.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.e2e.harness import (
    SubprocessResult,
    _init_spawn_registry_db,
    run_scripted_subprocess,
)


FIXTURES = Path(__file__).parent / "fixtures"

SESSION_ID = "agile-test-001"


def _read_snapshots(db_path: Path) -> list[dict]:
    """Return every ``agent_context_snapshots`` row from ``db_path``."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT agent_name, session_id, team_name, message_count, "
            "trigger, context_json FROM agent_context_snapshots "
            "ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _assert_has_assistant_message(snap: dict) -> None:
    """Parse snapshot.context_json and verify ≥1 assistant message present."""
    raw = json.loads(snap["context_json"])
    assert isinstance(raw, dict) and raw.get("messages"), (
        f"Snapshot context_json missing 'messages' list: "
        f"{snap['context_json'][:200]!r}"
    )
    messages = raw["messages"]
    assert len(messages) >= 1, (
        f"Expected ≥1 message in snapshot, got {len(messages)}"
    )


@pytest.mark.slow
def test_two_members_share_session_id_in_snapshots(tmp_path: Path):
    """PM + Dev spawn sequentially with the same TEAM_SESSION_ID — both
    snapshots must land in the shared DB tagged with that session_id and
    their own (distinct) agent_name / team_name values.
    """
    pm_env = {
        "TEAM_MY_NAME": "Linh [PM]",
        "TEAM_MY_ROLE": "pm",
        "TEAM_SESSION_ID": SESSION_ID,
        "TEAM_WORKSPACE": "",  # skip keep-alive loop
    }
    dev_env = {
        "TEAM_MY_NAME": "Khoi [Dev]",
        "TEAM_MY_ROLE": "dev",
        "TEAM_SESSION_ID": SESSION_ID,
        "TEAM_WORKSPACE": "",
    }

    pm_result = run_scripted_subprocess(
        fixture_path=FIXTURES / "team_pm_kickoff.yaml",
        task="Kick off the project",
        tmp_path=tmp_path,
        extra_env=pm_env,
        timeout=120,
    )
    dev_result = run_scripted_subprocess(
        fixture_path=FIXTURES / "team_dev_complete.yaml",
        task="Implement feature X",
        tmp_path=tmp_path,
        extra_env=dev_env,
        timeout=120,
    )

    assert pm_result.returncode == 0, (
        f"PM subprocess failed.\nSTDERR:\n{pm_result.stderr}"
    )
    assert dev_result.returncode == 0, (
        f"Dev subprocess failed.\nSTDERR:\n{dev_result.stderr}"
    )

    # Both calls share tmp_path → share jarvis.db. The second SubprocessResult
    # sees both rows.
    snaps = dev_result.context_snapshots()
    session_snaps = [s for s in snaps if s["session_id"] == SESSION_ID]
    assert len(session_snaps) == 2, (
        f"Expected 2 snapshots under session_id={SESSION_ID!r}, "
        f"got {len(session_snaps)}: "
        f"{[(s['agent_name'], s['session_id']) for s in snaps]}"
    )

    by_name = {s["agent_name"]: s for s in session_snaps}
    assert "Linh [PM]" in by_name, (
        f"PM snapshot missing. Agents seen: {list(by_name)}"
    )
    assert "Khoi [Dev]" in by_name, (
        f"Dev snapshot missing. Agents seen: {list(by_name)}"
    )
    assert by_name["Linh [PM]"]["team_name"] == "pm"
    assert by_name["Khoi [Dev]"]["team_name"] == "dev"

    for snap in session_snaps:
        _assert_has_assistant_message(snap)


@pytest.mark.slow
def test_parallel_dev_members_both_persist_snapshot(tmp_path: Path):
    """Two Dev members spawn in parallel with a shared SPAWN_REGISTRY_DB —
    both snapshots must land in the shared DB with distinct agent_names.

    Guards SQLite concurrent-insert behaviour for ``save_agent_context``.
    """
    shared_db = tmp_path / "shared.db"
    _init_spawn_registry_db(shared_db)

    def _spawn(name: str, subdir: str) -> SubprocessResult:
        sub_tmp = tmp_path / subdir
        sub_tmp.mkdir()
        return run_scripted_subprocess(
            fixture_path=FIXTURES / "team_dev_complete.yaml",
            task=f"Implement feature for {name}",
            tmp_path=sub_tmp,
            extra_env={
                "TEAM_MY_NAME": name,
                "TEAM_MY_ROLE": "dev",
                "TEAM_SESSION_ID": SESSION_ID,
                "TEAM_WORKSPACE": "",
                # Override per-call DB with the shared one initialised above.
                "SPAWN_REGISTRY_DB": str(shared_db),
            },
            timeout=120,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(_spawn, "Khoi [Dev]", "dev_a")
        fut_b = pool.submit(_spawn, "Nam [Dev]", "dev_b")
        res_a = fut_a.result()
        res_b = fut_b.result()

    assert res_a.returncode == 0, (
        f"Dev A subprocess failed.\nSTDERR:\n{res_a.stderr}"
    )
    assert res_b.returncode == 0, (
        f"Dev B subprocess failed.\nSTDERR:\n{res_b.stderr}"
    )

    snaps = _read_snapshots(shared_db)
    session_snaps = [s for s in snaps if s["session_id"] == SESSION_ID]
    assert len(session_snaps) == 2, (
        f"Expected 2 snapshots under session_id={SESSION_ID!r}, "
        f"got {len(session_snaps)}: "
        f"{[(s['agent_name'], s['session_id']) for s in snaps]}"
    )

    names = {s["agent_name"] for s in session_snaps}
    assert names == {"Khoi [Dev]", "Nam [Dev]"}, (
        f"Parallel Dev snapshots missing or duplicated. agent_names={names}"
    )
    assert all(s["team_name"] == "dev" for s in session_snaps), (
        f"team_name must be 'dev' for both. "
        f"got {[(s['agent_name'], s['team_name']) for s in session_snaps]}"
    )


@pytest.mark.slow
@pytest.mark.skip(
    reason=(
        "team_communicate is an MCP tool served by spawn/servers/"
        "team_communicate_server.py. Exercising it end-to-end requires "
        "wiring the server into the subprocess handoff config (mcp.servers "
        "entry + command/args resolution) which the current ScriptedLLM "
        "harness does not support. The MessageBus JSONL schema is covered "
        "by unit tests in the fast-agent fork — re-covering it here would "
        "duplicate without adding integration value."
    )
)
def test_team_communicate_message_persists_in_inbox_file(tmp_path: Path):
    """Planned: Dev sends team_communicate → PM, assert JSONL inbox schema."""


@pytest.mark.slow
def test_env_vars_propagate_to_subprocess_snapshot(tmp_path: Path):
    """Single Dev spawn — verify TEAM_MY_NAME / TEAM_MY_ROLE / TEAM_SESSION_ID
    each map to their snapshot column without silent drops.
    """
    env = {
        "TEAM_MY_NAME": "Nga [Dev]",
        "TEAM_MY_ROLE": "dev",
        "TEAM_SESSION_ID": "agile-envcheck-042",
        "TEAM_WORKSPACE": "",
    }

    result = run_scripted_subprocess(
        fixture_path=FIXTURES / "team_dev_complete.yaml",
        task="Verify env propagation",
        tmp_path=tmp_path,
        extra_env=env,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"Dev subprocess failed.\nSTDERR:\n{result.stderr}"
    )

    snaps = result.context_snapshots()
    assert len(snaps) == 1, (
        f"Expected exactly 1 snapshot, got {len(snaps)}: "
        f"{[(s['agent_name'], s['session_id'], s['team_name']) for s in snaps]}"
    )
    snap = snaps[0]
    assert snap["agent_name"] == "Nga [Dev]", (
        f"TEAM_MY_NAME → agent_name drift: got {snap['agent_name']!r}"
    )
    assert snap["team_name"] == "dev", (
        f"TEAM_MY_ROLE → team_name drift: got {snap['team_name']!r}"
    )
    assert snap["session_id"] == "agile-envcheck-042", (
        f"TEAM_SESSION_ID → session_id drift: got {snap['session_id']!r}"
    )
