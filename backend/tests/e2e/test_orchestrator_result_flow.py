"""E2E for the orchestrator-final-response flow.

Models the post-2026-05-13 contract: ScriptedLLM drives a fake
orchestrator (Morgan [PM]) through one turn that emits a roll-up
markdown block. The test then walks every downstream consumer that
the 2026-05-13 incident broke and asserts they all surface the same
roll-up text — no silent fallbacks, no error_state.

What this proves end-to-end:

1. **Write path** — ``save_agent_context`` mirrors the last assistant
   text into ``spawn_registry.data_json.result`` (single source of
   truth for orchestrator output).
2. **Read path: ``get_team_result``** — returns the roll-up via
   ``agents[name].result`` and omits the ``error_state`` block.
3. **Notification body** — ``_create_team_notification`` renders the
   roll-up verbatim in the DB notification, with no BUG indicator
   in title / preview / content.
4. **Status semantics** — ``get_team_status`` reports a stuck agent
   loudly (``status="stuck"``, ``sprint_status="stuck"``) when the
   registry says it's idle but last_active_at is stale.

Uses real SQLite (no ORM mocks). The only "fake" piece is the LLM
itself, which ScriptedLLM replays from the YAML fixture.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from tests.e2e.harness import build_scripted_agent


FIXTURES = Path(__file__).parent / "fixtures"


# ── DB fixture helpers ──────────────────────────────────────────────


def _init_schema(db_path: Path) -> None:
    """Create the three tables our flow touches.

    These are normally created by ``services.context_persistence``
    (snapshots), ``fast_agent.spawn.registry_backends.SqliteBackend``
    (spawn_registry), and ``TeamSessionStore`` (team_sessions). We
    create them up front so the test can pre-seed rows without
    racing the production code's CREATE TABLE IF NOT EXISTS calls.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS spawn_registry (
                run_id TEXT PRIMARY KEY,
                data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS team_sessions (
                session_id TEXT PRIMARY KEY,
                data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_context_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                session_id TEXT,
                team_name TEXT,
                context_json TEXT NOT NULL,
                message_count INTEGER DEFAULT 0,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                trigger TEXT DEFAULT 'manual',
                created_at REAL NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _seed_orchestrator(db_path: Path, *, run_id: str, agent_name: str,
                        session_id: str, team_name: str) -> None:
    """Pre-create the spawn_registry + team_sessions rows.

    Matches the shape the real spawner would write — minimum keys
    needed for the read-side consumers to find the orchestrator.
    """
    spawn_row = {
        "run_id": run_id,
        "agent_name": agent_name,
        "role": "pm",
        "status": "running",
        "lifecycle": "resumable",
        "team_name": team_name,
        "result": "",
        "started_at": time.time(),
        "last_active_at": time.time(),
    }
    team_row = {
        "session_id": session_id,
        "template": {"name": "agile-team", "roles": {}},
        "workspace": "/tmp/audit-ws",
        "project_brief": "self-audit",
        "parent_session_id": "",
        "team_name": team_name,
        "conversation_id": "",
        "agents": {
            agent_name: {
                "run_id": run_id,
                "role": "pm",
                "agent_name": agent_name,
                "status": "running",
            },
        },
        "sprint_status": "orchestrator_running",
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO spawn_registry (run_id, data_json) VALUES (?, ?)",
            (run_id, json.dumps(spawn_row, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO team_sessions (session_id, data_json) VALUES (?, ?)",
            (session_id, json.dumps(team_row, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def _read_spawn_registry(db_path: Path, run_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT data_json FROM spawn_registry WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"spawn_registry row {run_id!r} not found"
    return json.loads(row[0])


# ── E2E tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_rollup_flows_through_full_consumer_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whole stack: scripted PM emits roll-up → mirror writes it →
    notification + get_team_result both surface it verbatim.

    Failure of any link manifests as either an empty
    ``spawn_registry.result`` (mirror broken), a notification body
    containing the BUG indicator (read path falling loud against a
    populated registry — would mean we read the wrong column), or an
    ``error_state`` in ``get_team_result`` (same).
    """
    db_path = tmp_path / "jarvis.db"
    _init_schema(db_path)
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(db_path))

    run_id = "run-pm-e2e"
    agent_name = "Morgan [PM]"
    session_id = "sess-audit-e2e"
    team_name = "audit-team-e2e"
    _seed_orchestrator(
        db_path, run_id=run_id, agent_name=agent_name,
        session_id=session_id, team_name=team_name,
    )

    # 1. Drive a scripted agent through one orchestrator-style turn.
    agent = await build_scripted_agent(
        fixture_path=FIXTURES / "orchestrator_rollup.yaml",
        tools=[],
        agent_name="orchestrator",
        instruction="You are Morgan [PM], the audit orchestrator.",
    )
    final = await agent.generate("Run the self-audit aggregation.")

    # Sanity: the scripted final message contains the roll-up header.
    final_text = "".join(
        c.text for c in (final.content or [])
        if getattr(c, "type", "") == "text"
    )
    assert "Team Roll-up" in final_text, (
        "Scripted agent did not produce the roll-up text — fixture "
        f"may be malformed.\nGot:\n{final_text[:400]}"
    )

    # 2. Fire the lifecycle hook that production runs on turn-end.
    #    The agent has a fresh in-process history; we feed it through
    #    save_agent_context exactly the way isolated_runner does.
    from services.context_persistence import save_agent_context
    snap_id = await save_agent_context(
        agent, run_id, "idle",
        agent_name=agent_name,
        session_id=session_id,
        team_name=team_name,
    )
    assert snap_id, "save_agent_context must have written a snapshot row"

    # 3. The mirror must have populated spawn_registry.result.
    stored = _read_spawn_registry(db_path, run_id)
    assert "Team Roll-up" in stored["result"], (
        "Mirror did not write the orchestrator's final text into "
        f"spawn_registry.result.\nresult={stored.get('result')!r}"
    )
    assert stored["agent_name"] == agent_name, "merge-upsert lost other fields"
    assert stored["team_name"] == team_name
    assert "result_updated_at" in stored, "mirror must stamp the write time"

    # 4. Read path #1 — get_team_result returns the roll-up, no error_state.
    from fast_agent.spawn.servers import agent_spawner_server as srv
    from fast_agent.spawn.spawn_registry import SpawnRegistry
    # Reset cached singletons so they pick up our temp DB path. Without
    # this, a sibling test's earlier import has cached an empty _data
    # snapshot and the read-side sync sees no result even though the
    # write half wrote one.
    import fast_agent.spawn.team_spawner as ts
    monkeypatch.setattr(ts, "_team_store", None)
    fresh_registry = SpawnRegistry(registry_file=str(tmp_path / "unused.json"))
    monkeypatch.setattr(srv, "_registry", fresh_registry)
    payload = json.loads(srv.get_team_result(session_id))
    assert "error_state" not in payload, (
        f"orchestrator_result_missing fired against a populated registry: "
        f"{payload.get('error_state')!r}"
    )
    assert "Team Roll-up" in payload["agents"][agent_name]["result"]

    # 5. Read path #2 — _create_team_notification renders the roll-up
    #    as the notification body (no BUG indicator).
    from unittest.mock import MagicMock, patch as _patch
    from services.spawn_progress_bridge import SpawnProgressBridge

    bridge = SpawnProgressBridge(
        progress_manager=MagicMock(), registry_db=None,
    )
    captured = {}

    class _Notif:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.id = 1

    with _patch("core.database.NotificationModel", _Notif), \
            _patch("core.database.get_db_session", return_value=MagicMock()), \
            _patch("services.cron_scheduler.scheduler_stream_manager", MagicMock()):
        bridge._create_team_notification(
            team_name=team_name,
            agent_name=agent_name,
            result=stored["result"],
            members=[{"agent_name": agent_name, "status": "idle"}],
        )

    assert "BUG" not in captured["preview"], (
        "Fail-loud body leaked into the happy path — read path is "
        "ignoring spawn_registry.result"
    )
    assert "Team Roll-up" in captured["content"]
    assert captured["title"].startswith("✅"), "title must mark success"


@pytest.mark.asyncio
async def test_hung_running_agent_surfaced_as_status_stuck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end stuck-detection: an agent whose registry status is
    ``running`` but whose ``last_active_at`` is older than the
    threshold (LLM call / tool dispatch is hung) must surface as
    ``sprint_status="stuck"`` with per-agent ``status="stuck"``.

    Important counter-case: a long-idle resumable agent is NOT stuck.
    That branch is covered by the unit suite
    (``test_idle_is_never_reclassified_as_stuck_no_matter_how_old``);
    here we just pin the genuine hang.
    """
    db_path = tmp_path / "jarvis.db"
    _init_schema(db_path)
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(db_path))

    run_id = "run-hang-e2e"
    agent_name = "Hung [Dev]"
    session_id = "sess-hang-e2e"
    team_name = "hang-team-e2e"

    # Seed: registry says running with stale last_active (LLM hang).
    spawn_row = {
        "run_id": run_id,
        "agent_name": agent_name,
        "role": "dev",
        "status": "running",
        "lifecycle": "resumable",
        "team_name": team_name,
        "result": "",
        "started_at": time.time() - 1300,
        "last_active_at": time.time() - 1200,
    }
    team_row = {
        "session_id": session_id,
        "template": {"name": "agile-team", "roles": {}},
        "workspace": "/tmp/hang-ws",
        "project_brief": "demo",
        "parent_session_id": "",
        "team_name": team_name,
        "conversation_id": "",
        "agents": {
            agent_name: {
                "run_id": run_id, "role": "dev",
                "agent_name": agent_name, "status": "running",
            },
        },
        "sprint_status": "running",
    }
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO spawn_registry (run_id, data_json) VALUES (?, ?)",
        (run_id, json.dumps(spawn_row)),
    )
    conn.execute(
        "INSERT INTO team_sessions (session_id, data_json) VALUES (?, ?)",
        (session_id, json.dumps(team_row)),
    )
    conn.commit()
    conn.close()

    # Bypass the team_store singleton's lazy init so it picks up our temp DB.
    import fast_agent.spawn.team_spawner as ts
    monkeypatch.setattr(ts, "_team_store", None)

    # And give the spawner server a registry that resolves by run_id.
    from fast_agent.spawn.servers import agent_spawner_server as srv
    from fast_agent.spawn.spawn_registry import SpawnRegistry
    real_registry = SpawnRegistry(
        registry_file=str(tmp_path / "unused.json"),
    )
    monkeypatch.setattr(srv, "_registry", real_registry)

    payload = json.loads(srv.get_team_status(session_id))

    dev = payload["agents"][agent_name]
    assert dev["status"] == "stuck", (
        f"hung-running agent must surface as 'stuck', got {dev['status']!r}"
    )
    assert dev["raw_status"] == "running"
    assert dev["stuck_seconds"] >= 30
    assert payload["sprint_status"] == "stuck"
    assert "1 stuck" in payload["progress"]
