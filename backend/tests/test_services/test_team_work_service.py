"""Revisioned team directives across independent sessions."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.database import engine, init_db
from services import team_work_service as work


@pytest.fixture()
def teams(tmp_path: Path, monkeypatch):
    from fast_agent.spawn import team_spawner
    import services.shared_state as state

    init_db()
    with sqlite3.connect(engine.url.database) as conn:
        conn.execute("DELETE FROM team_requirement_revisions")
        conn.execute("DELETE FROM team_work_bindings")

    sessions = {}
    records = {}
    for session_id, agent in (("team-a", "Alex"), ("team-b", "Bailey")):
        sessions[session_id] = SimpleNamespace(
            session_id=session_id,
            team_name=session_id,
            project_brief=f"Build {session_id}",
            template={"orchestrator": "pm"},
            agents={agent: {"role": "pm", "run_id": f"run-{session_id}"}},
        )
        records[f"run-{session_id}"] = {
            "original_config": {"env_vars": {
                "TEAM_MESSAGES_DIR": str(tmp_path / "messages" / session_id)
            }}
        }
    monkeypatch.setattr(team_spawner, "get_team_session", sessions.get)
    monkeypatch.setattr(state, "registry_db", SimpleNamespace(
        get_record=records.get
    ))
    with patch("fast_agent.spawn.servers._team_helpers.auto_wake_if_idle"):
        yield sessions, records, tmp_path


@pytest.mark.asyncio
async def test_two_teams_receive_only_their_own_revisions(teams):
    from fast_agent.spawn.message_bus import MessageBus

    _sessions, _records, root = teams
    work.bind_team("team-a", "chat-1")
    work.bind_team("team-b", "chat-1")
    first = work.create_revision("team-a", "chat-1", "Add export", expected_revision=0)
    second = work.create_revision("team-b", "chat-1", "Change colors", expected_revision=0)
    await work.deliver_revision(first["id"])
    await work.deliver_revision(second["id"])

    inbox_a = MessageBus(root / "messages" / "team-a").read_unread("Alex")
    inbox_b = MessageBus(root / "messages" / "team-b").read_unread("Bailey")
    assert len(inbox_a) == len(inbox_b) == 1
    assert "Add export" in inbox_a[0].content
    assert "Change colors" in inbox_b[0].content
    assert inbox_a[0].context["revision"] == inbox_b[0].context["revision"] == 1


def test_idempotency_and_stale_revision_are_explicit(teams):
    work.bind_team("team-a", "chat-1")
    first = work.create_revision(
        "team-a", "chat-1", "Add export", idempotency_key="turn-1"
    )
    repeated = work.create_revision(
        "team-a", "chat-1", "Add export", idempotency_key="turn-1"
    )
    assert repeated["id"] == first["id"]
    with pytest.raises(work.TeamWorkError, match="different change"):
        work.create_revision("team-a", "chat-1", "Remove export",
                             idempotency_key="turn-1")
    with pytest.raises(work.TeamWorkError, match="current revision is 1"):
        work.create_revision("team-a", "chat-1", "Another change",
                             expected_revision=0)
    second = work.create_revision("team-a", "chat-1", "Another change",
                                  expected_revision=1)
    assert second["revision"] == 2
    latest = work.get_team_revisions("team-a", "chat-1", limit=1)
    assert [item["revision"] for item in latest] == [2]
    older = work.get_team_revisions(
        "team-a", "chat-1", limit=1, before_revision=2
    )
    assert [item["revision"] for item in older] == [1]


def test_conversation_scope_blocks_cross_team_changes(teams):
    work.bind_team("team-a", "chat-1")
    with pytest.raises(work.TeamWorkError) as exc:
        work.create_revision("team-a", "chat-2", "Wrong target")
    assert exc.value.status == 403
    assert work.list_teams("chat-2") == []


@pytest.mark.asyncio
async def test_retry_after_append_does_not_duplicate_message(teams):
    from fast_agent.spawn.message_bus import MessageBus

    _sessions, _records, root = teams
    work.bind_team("team-a", "chat-1")
    change = work.create_revision("team-a", "chat-1", "Keep acceptance criteria")

    # Simulate a crash after the JSONL append but before the DB status update.
    message_id, _ = work._queue_once(change)
    delivered = await work.deliver_revision(change["id"])

    inbox = MessageBus(root / "messages" / "team-a").read_inbox("Alex")
    assert len(inbox) == 1
    assert inbox[0].message_id == delivered["message_id"] == message_id


@pytest.mark.asyncio
async def test_missing_inbox_stays_pending_until_explicit_retry(teams):
    _sessions, records, _root = teams
    work.bind_team("team-a", "chat-1")
    change = work.create_revision("team-a", "chat-1", "Add test")
    env = records["run-team-a"]["original_config"]["env_vars"]
    inbox = env.pop("TEAM_MESSAGES_DIR")

    with pytest.raises(work.TeamWorkError, match="inbox is unavailable"):
        await work.deliver_revision(change["id"])
    assert work.get_team_revisions("team-a", "chat-1")[0]["status"] == "pending"

    env["TEAM_MESSAGES_DIR"] = inbox
    delivered = await work.retry_pending("team-a", "chat-1")
    assert len(delivered) == 1
    assert delivered[0]["status"] == "delivered"


@pytest.mark.asyncio
async def test_startup_replay_recovers_pending_without_duplicate(teams):
    from fast_agent.spawn.message_bus import MessageBus

    _sessions, _records, root = teams
    work.bind_team("team-a", "chat-1")
    change = work.create_revision("team-a", "chat-1", "Add export")
    work._queue_once(change)  # Simulate crash after append.

    await work.replay_pending_on_startup()
    await work.replay_pending_on_startup()

    inbox = MessageBus(root / "messages" / "team-a").read_inbox("Alex")
    assert len(inbox) == 1
    assert work.get_team_revisions("team-a", "chat-1")[0]["status"] == "delivered"
