"""Replay the 2026-09-29 production resume failure through the real MCP tool."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fast_agent.spawn.spawn_registry import SpawnRecord


@pytest.mark.asyncio
async def test_production_idle_team_is_resumed(monkeypatch, tmp_path):
    from fast_agent.spawn.servers import agent_spawner_server as srv
    from fast_agent.spawn.agent_channel import AgentChannel
    from fast_agent.spawn.team_spawner import TeamSession

    evidence = (
        Path(__file__).parents[3] / "docs/evidence/team-resume/production-resume.json"
    )
    recorded = json.loads(evidence.read_text())["calls"][-1]["result"]
    monkeypatch.setattr(srv, "_PROJECT_DIR", tmp_path)
    session = TeamSession(
        "52051546", {}, Path("/tmp"), "audit", team_name=recorded["team_name"]
    )
    records = {}
    for name, row in recorded["agents"].items():
        available = "not found" in row["reason"]
        session.agents[name] = {
            "run_id": name,
            "status": "available" if available else "idle",
        }
        if not available:
            records[name] = SpawnRecord(
                run_id=name, agent_name=name, status="idle", lifecycle="resumable"
            )
    registry = MagicMock()
    registry.get.side_effect = records.get
    registry.get_latest.side_effect = records.get
    resume = AsyncMock(
        side_effect=lambda rid, task: json.dumps(
            {"status": "resumed", "new_run_id": "new-" + rid}
        )
    )
    monkeypatch.setattr(srv, "get_team_session", lambda sid: session)
    monkeypatch.setattr(srv, "_registry", registry)
    monkeypatch.setattr(srv, "_get_team_store", MagicMock())
    monkeypatch.setattr(srv, "resume_spawn", resume)
    monkeypatch.setattr(AgentChannel, "is_alive", lambda *a, **kw: False)
    result = json.loads(await srv.resume_team_tool("52051546", "Write revision 4"))
    assert result["resumed_agents"] == 4, result
    assert resume.await_count == 4
    assert result["status"] == "resumed"


@pytest.fixture
def resume_case(monkeypatch, tmp_path):
    from fast_agent.spawn.servers import agent_spawner_server as srv
    from fast_agent.spawn.team_spawner import TeamSession

    session = TeamSession("test-session", {}, tmp_path, "test", team_name="Test")
    session.sprint_status = "completed"
    session.agents = {"Dev": {"run_id": "old", "status": "idle"}}
    record = SpawnRecord(
        run_id="old",
        agent_name="Dev",
        status="idle",
        session_id="test-session",
        lifecycle="resumable",
    )
    registry = MagicMock()
    registry.get_latest.return_value = record
    monkeypatch.setattr(srv, "_registry", registry)
    monkeypatch.setattr(srv, "_PROJECT_DIR", tmp_path)
    monkeypatch.setattr(srv, "get_team_session", lambda _: session)
    store = MagicMock()
    monkeypatch.setattr(srv, "_get_team_store", lambda: store)
    resume = AsyncMock(
        return_value=json.dumps({"status": "resumed", "new_run_id": "new"})
    )
    monkeypatch.setattr(srv, "resume_spawn", resume)
    monkeypatch.setenv("SPAWN_PROJECT_DIR", str(tmp_path))
    return srv, session, record, resume, store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["idle", "completed", "error", "cancelled", "timeout"]
)
async def test_resumable_states(resume_case, state):
    srv, session, record, resume, store = resume_case
    record.status = state
    result = json.loads(await srv.resume_team_tool("test-session", "follow-up"))
    assert result["resumed_agents"] == 1
    assert session.agents["Dev"]["run_id"] == "new"
    store.upsert.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "pending", "paused"])
async def test_active_members_not_duplicated_or_false_success(resume_case, state):
    srv, session, record, resume, store = resume_case
    record.status = state
    result = json.loads(await srv.resume_team_tool("test-session", "follow-up"))
    assert result["status"] == "not_resumed"
    assert session.sprint_status == "completed"
    resume.assert_not_awaited()
    store.upsert.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["missing", "oneshot", "other-agent", "other-session", "snapshot", "exception"],
)
async def test_failures_are_explicit(resume_case, failure):
    srv, session, record, resume, store = resume_case
    if failure == "missing":
        srv._registry.get_latest.return_value = None
    elif failure == "oneshot":
        record.lifecycle = "oneshot"
    elif failure == "other-agent":
        record.agent_name = "Other"
    elif failure == "other-session":
        record.session_id = "other"
    elif failure == "snapshot":
        resume.return_value = json.dumps({"error": "No conversation snapshot"})
    else:
        resume.side_effect = RuntimeError("launch failed")
    result = json.loads(await srv.resume_team_tool("test-session", "follow-up"))
    assert result["status"] == "not_resumed"
    assert result["failed_agents"] == 1
    store.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_live_idle_uses_real_inbox_and_socket(resume_case, monkeypatch, tmp_path):
    import asyncio
    from fast_agent.spawn.agent_channel import AgentChannel
    from fast_agent.spawn.message_bus import MessageBus

    srv, session, record, resume, store = resume_case
    channel_dir = tmp_path / "channels"
    channel = AgentChannel("Dev", channel_dir, session_id="test-session", run_id="old")
    await channel.start_server()
    alive, signal = AgentChannel.is_alive, AgentChannel.send_signal
    monkeypatch.setattr(
        AgentChannel,
        "is_alive",
        lambda name, **scope: alive(name, channel_dir, **scope),
    )
    monkeypatch.setattr(
        AgentChannel,
        "send_signal",
        lambda name, sig, **scope: signal(name, sig, channel_dir, **scope),
    )
    record.original_config = {
        "env_vars": {"TEAM_MESSAGES_DIR": str(tmp_path / "inbox")}
    }
    try:
        result = json.loads(
            await srv.resume_team_tool("test-session", "unique follow-up")
        )
        assert result["status"] == "queued"
        assert result["queued_agents"] == 1
        assert await asyncio.wait_for(channel.listen(), 2) == "wake"
        messages = MessageBus(tmp_path / "inbox").read_unread("Dev")
        assert [m.content for m in messages] == ["unique follow-up"]
        resume.assert_not_awaited()
        assert session.agents["Dev"]["run_id"] == "old"
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_wake_failure_reports_saved_message(resume_case, monkeypatch):
    from fast_agent.spawn.agent_channel import AgentChannel

    srv, session, record, resume, store = resume_case
    monkeypatch.setattr(AgentChannel, "is_alive", lambda *a, **kw: True)
    monkeypatch.setattr(AgentChannel, "send_signal", lambda *a, **kw: False)
    result = json.loads(await srv.resume_team_tool("test-session", "follow-up"))
    assert result["status"] == "not_resumed"
    assert result["agents"]["Dev"]["message_queued"] is True
    resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_resume_is_rejected(resume_case):
    import asyncio

    srv, session, record, resume, store = resume_case
    entered, release = asyncio.Event(), asyncio.Event()

    async def launch(*args):
        entered.set()
        await release.wait()
        return json.dumps({"status": "resumed", "new_run_id": "new"})

    resume.side_effect = launch
    first = asyncio.create_task(srv.resume_team_tool("test-session", "first"))
    await entered.wait()
    try:
        second = json.loads(await srv.resume_team_tool("test-session", "second"))
        assert second["status"] == "busy"
        assert resume.await_count == 1
    finally:
        release.set()
        await first


@pytest.mark.asyncio
async def test_partial_resume_preserves_errors(resume_case):
    srv, session, record, resume, store = resume_case
    session.agents["Missing"] = {"run_id": "missing", "status": "idle"}
    srv._registry.get_latest.side_effect = lambda rid: record if rid == "old" else None
    result = json.loads(await srv.resume_team_tool("test-session", "follow-up"))
    assert (result["status"], result["resumed_agents"], result["failed_agents"]) == (
        "partial",
        1,
        1,
    )


@pytest.mark.asyncio
async def test_empty_request_and_missing_session(resume_case, monkeypatch):
    srv, session, record, resume, store = resume_case
    assert (
        json.loads(await srv.resume_team_tool("test-session", " "))["status"]
        == "not_resumed"
    )
    monkeypatch.setattr(srv, "get_team_session", lambda _: None)
    assert (
        json.loads(await srv.resume_team_tool("missing", "task"))["status"]
        == "not_resumed"
    )
    resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_dead_idle_restores_sqlite_snapshot_and_identity(monkeypatch, tmp_path):
    """Real tool -> resume_spawn -> SQLite history; substitute only LLM launch."""
    import sqlite3
    from fast_agent.spawn.servers import agent_spawner_server as srv
    from fast_agent.spawn.spawn_registry import SpawnRegistry
    from fast_agent.spawn.team_spawner import TeamSession
    from services import context_persistence

    db = tmp_path / "runtime.db"
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(db))
    monkeypatch.setenv("SPAWN_PROJECT_DIR", str(tmp_path))
    registry = SpawnRegistry(registry_file=str(tmp_path / "registry.json"))
    session = TeamSession(
        "saved-team", {}, tmp_path, "original brief", team_name="Audit"
    )
    session.agents = {
        "Toby [PM]": {"run_id": "old-run", "role": "pm", "status": "idle"}
    }
    cfg = {
        "agent_name": "Toby [PM]",
        "role": "pm",
        "project_dir": str(tmp_path),
        "workspace_dir": str(tmp_path / "workspace"),
        "skills": ["audit"],
        "server_overrides": {"filesystem": {"args": ["workspace"]}},
        "env_vars": {
            "TEAM_SESSION_ID": "saved-team",
            "TEAM_MESSAGES_DIR": str(tmp_path / "messages"),
        },
    }
    registry.register(
        SpawnRecord(
            run_id="old-run",
            agent_name="Toby [PM]",
            role="pm",
            team_name="Audit",
            session_id="saved-team",
            lifecycle="resumable",
            status="idle",
            original_config=cfg,
        )
    )
    snapshot = json.dumps(
        [
            {
                "role": "user",
                "content": [{"type": "text", "text": "remember original context"}],
            }
        ]
    )
    conn = sqlite3.connect(db)
    context_persistence._ensure_table(conn)
    conn.execute(
        "INSERT INTO agent_context_snapshots (run_id,agent_name,session_id,context_json,trigger,created_at) VALUES (?,?,?,?,?,?)",
        ("old-run", "Toby [PM]", "saved-team", snapshot, "idle", 1),
    )
    conn.commit()
    conn.close()
    captured = {}

    async def launch(**kwargs):
        captured.update(kwargs)
        history = Path(kwargs["history_file"])
        captured["restored_history"] = history.read_text()
        history.unlink()  # test owns the materialized temporary snapshot
        registry.register(
            SpawnRecord(
                run_id="new-run",
                agent_name="Toby [PM]",
                session_id="saved-team",
                status="running",
                lifecycle="resumable",
            )
        )
        return "new-run"

    monkeypatch.setattr(srv, "run_isolated_agent_background", launch)
    monkeypatch.setattr(srv, "_registry", registry)
    monkeypatch.setattr(srv, "_PROJECT_DIR", tmp_path)
    monkeypatch.setattr(srv, "get_team_session", lambda _: session)
    store = MagicMock()
    monkeypatch.setattr(srv, "_get_team_store", lambda: store)
    result = json.loads(await srv.resume_team_tool("saved-team", "write revision 4"))
    assert result["status"] == "resumed"
    assert captured["restored_history"] == snapshot
    assert captured["session_id"] == "saved-team"
    for key in [
        "agent_name",
        "workspace_dir",
        "skills",
        "server_overrides",
        "env_vars",
    ]:
        assert captured[key] == cfg[key]
    assert captured["task"] == "write revision 4"
    assert registry.get_latest("old-run").run_id == "new-run"
    assert session.agents["Toby [PM]"]["run_id"] == "new-run"
    # A repeat call sees the running successor, not the stale idle predecessor.
    again = json.loads(await srv.resume_team_tool("saved-team", "second request"))
    assert again["status"] == "not_resumed"
