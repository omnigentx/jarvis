"""Tests for TeamSession persistence refactor.

Covers:
- TeamSessionStore: CRUD, auto-create table, upsert idempotency
- create_team_store(): RuntimeError without SPAWN_REGISTRY_DB
- TeamSession: project_brief required, from_dict/to_dict roundtrip, strict KeyError
- get_team_session(): cache hit, DB fallback, not-found, corrupt-delete
- list_team_sessions(): store + cache merged, in-memory authoritative
- spawn_team_members_for_session(): ValueError when brief and first_task both empty
"""

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_store(tmp_path):
    from fast_agent.spawn.registry_backends import TeamSessionStore
    db_path = str(tmp_path / "test.db")
    return TeamSessionStore(db_path)


def _minimal_session_dict(**overrides):
    base = {
        "session_id": "ses-001",
        "template": {"name": "sprint", "roles": {}},
        "workspace": "/tmp/ws",
        "project_brief": "Build a REST API",
        "parent_session_id": "",
        "team_name": "sprint",
        "conversation_id": "",
        "agents": {},
        "sprint_status": "pending",
    }
    base.update(overrides)
    return base


# ── TeamSessionStore ──────────────────────────────────────────────────────────


class TestTeamSessionStore:
    def test_create_team_store_raises_without_env(self, monkeypatch):
        monkeypatch.delenv("SPAWN_REGISTRY_DB", raising=False)
        from fast_agent.spawn.registry_backends import create_team_store
        with pytest.raises(RuntimeError, match="SPAWN_REGISTRY_DB"):
            create_team_store()

    def test_create_team_store_succeeds_with_env(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / "test.db")
        monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)
        from fast_agent.spawn.registry_backends import create_team_store
        store = create_team_store()
        assert store is not None

    def test_init_creates_table(self, tmp_path):
        store = _make_store(tmp_path)
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "team_sessions" in tables

    def test_upsert_and_get(self, tmp_path):
        store = _make_store(tmp_path)
        data = _minimal_session_dict()
        store.upsert("ses-001", data)
        result = store.get("ses-001")
        assert result is not None
        assert result["session_id"] == "ses-001"
        assert result["project_brief"] == "Build a REST API"

    def test_get_returns_none_for_missing(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.get("nonexistent") is None

    def test_upsert_overwrites(self, tmp_path):
        store = _make_store(tmp_path)
        store.upsert("ses-001", _minimal_session_dict(sprint_status="pending"))
        store.upsert("ses-001", _minimal_session_dict(sprint_status="running"))
        assert store.get("ses-001")["sprint_status"] == "running"

    def test_list_all_returns_all_records(self, tmp_path):
        store = _make_store(tmp_path)
        store.upsert("ses-001", _minimal_session_dict(session_id="ses-001"))
        store.upsert("ses-002", _minimal_session_dict(session_id="ses-002"))
        records = store.list_all()
        ids = {r["session_id"] for r in records}
        assert ids == {"ses-001", "ses-002"}

    def test_list_all_empty_when_no_records(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.list_all() == []

    def test_delete_removes_record(self, tmp_path):
        store = _make_store(tmp_path)
        store.upsert("ses-001", _minimal_session_dict())
        store.delete("ses-001")
        assert store.get("ses-001") is None

    def test_delete_nonexistent_is_no_op(self, tmp_path):
        store = _make_store(tmp_path)
        store.delete("nonexistent")  # should not raise

    def test_upsert_preserves_unicode(self, tmp_path):
        store = _make_store(tmp_path)
        data = _minimal_session_dict(project_brief="Xây dựng API cho người dùng")
        store.upsert("ses-001", data)
        result = store.get("ses-001")
        assert result["project_brief"] == "Xây dựng API cho người dùng"

    def test_multiple_upserts_dont_duplicate_in_list(self, tmp_path):
        store = _make_store(tmp_path)
        store.upsert("ses-001", _minimal_session_dict())
        store.upsert("ses-001", _minimal_session_dict(sprint_status="done"))
        assert len(store.list_all()) == 1


# ── TeamSession ───────────────────────────────────────────────────────────────


class TestTeamSession:
    def test_requires_project_brief(self):
        from fast_agent.spawn.team_spawner import TeamSession
        with pytest.raises(TypeError):
            TeamSession(
                session_id="ses-001",
                template={},
                workspace=Path("/tmp"),
                # project_brief missing → TypeError
            )

    def test_project_brief_stored(self):
        from fast_agent.spawn.team_spawner import TeamSession
        s = TeamSession("ses-001", {}, Path("/tmp"), project_brief="brief here")
        assert s.project_brief == "brief here"

    def test_to_dict_includes_project_brief(self):
        from fast_agent.spawn.team_spawner import TeamSession
        s = TeamSession("ses-001", {"name": "sprint"}, Path("/tmp/ws"), "my brief")
        d = s.to_dict()
        assert d["project_brief"] == "my brief"
        assert d["session_id"] == "ses-001"
        assert d["workspace"] == "/tmp/ws"

    def test_from_dict_roundtrip(self):
        from fast_agent.spawn.team_spawner import TeamSession
        original = TeamSession(
            session_id="ses-42",
            template={"name": "dev-team", "roles": {"dev": {}, "ba": {}}},
            workspace=Path("/tmp/workspaces/dev-team"),
            project_brief="Implement OAuth2 flow",
            parent_session_id="parent-abc",
            team_name="dev-team",
            conversation_id="conv-xyz",
        )
        original.agents = {"Minh [Dev]": {"run_id": "run-1", "role": "dev", "status": "running"}}
        original.sprint_status = "orchestrator_running"

        restored = TeamSession.from_dict(original.to_dict())

        assert restored.session_id == "ses-42"
        assert restored.project_brief == "Implement OAuth2 flow"
        assert restored.workspace == Path("/tmp/workspaces/dev-team")
        assert restored.team_name == "dev-team"
        assert restored.conversation_id == "conv-xyz"
        assert restored.agents["Minh [Dev]"]["run_id"] == "run-1"
        assert restored.sprint_status == "orchestrator_running"

    def test_from_dict_workspace_is_path(self):
        from fast_agent.spawn.team_spawner import TeamSession
        d = _minimal_session_dict()
        s = TeamSession.from_dict(d)
        assert isinstance(s.workspace, Path)

    def test_from_dict_raises_keyerror_on_missing_session_id(self):
        from fast_agent.spawn.team_spawner import TeamSession
        d = _minimal_session_dict()
        del d["session_id"]
        with pytest.raises(KeyError):
            TeamSession.from_dict(d)

    def test_from_dict_raises_keyerror_on_missing_project_brief(self):
        from fast_agent.spawn.team_spawner import TeamSession
        d = _minimal_session_dict()
        del d["project_brief"]
        with pytest.raises(KeyError):
            TeamSession.from_dict(d)

    def test_from_dict_raises_keyerror_on_missing_workspace(self):
        from fast_agent.spawn.team_spawner import TeamSession
        d = _minimal_session_dict()
        del d["workspace"]
        with pytest.raises(KeyError):
            TeamSession.from_dict(d)

    def test_from_dict_raises_keyerror_on_missing_agents(self):
        from fast_agent.spawn.team_spawner import TeamSession
        d = _minimal_session_dict()
        del d["agents"]
        with pytest.raises(KeyError):
            TeamSession.from_dict(d)

    def test_from_dict_optional_fields_default(self):
        from fast_agent.spawn.team_spawner import TeamSession
        d = _minimal_session_dict()
        d.pop("parent_session_id", None)
        d.pop("conversation_id", None)
        s = TeamSession.from_dict(d)
        assert s.parent_session_id == ""
        assert s.conversation_id == ""

    def test_team_name_defaults_to_template_name(self):
        from fast_agent.spawn.team_spawner import TeamSession
        s = TeamSession("id", {"name": "sprint-template"}, Path("/tmp"), "brief")
        assert s.team_name == "sprint-template"


# ── get_team_session ──────────────────────────────────────────────────────────


class TestGetTeamSession:
    def test_returns_from_cache(self, monkeypatch):
        import fast_agent.spawn.team_spawner as ts_module
        from fast_agent.spawn.team_spawner import TeamSession

        session = TeamSession("ses-cached", {}, Path("/tmp"), "brief")
        monkeypatch.setattr(ts_module, "_team_sessions", {"ses-cached": session})

        store_mock = MagicMock()
        monkeypatch.setattr(ts_module, "_team_store", store_mock)

        from fast_agent.spawn.team_spawner import get_team_session
        result = get_team_session("ses-cached")

        assert result is session
        store_mock.get.assert_not_called()  # cache hit, no DB access

    def test_falls_back_to_db(self, tmp_path, monkeypatch):
        import fast_agent.spawn.team_spawner as ts_module
        from fast_agent.spawn.registry_backends import TeamSessionStore

        store = TeamSessionStore(str(tmp_path / "test.db"))
        store.upsert("ses-db", _minimal_session_dict(session_id="ses-db"))

        monkeypatch.setattr(ts_module, "_team_sessions", {})
        monkeypatch.setattr(ts_module, "_team_store", store)

        from fast_agent.spawn.team_spawner import get_team_session
        result = get_team_session("ses-db")

        assert result is not None
        assert result.session_id == "ses-db"
        assert result.project_brief == "Build a REST API"
        assert "ses-db" in ts_module._team_sessions

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        import fast_agent.spawn.team_spawner as ts_module
        from fast_agent.spawn.registry_backends import TeamSessionStore

        store = TeamSessionStore(str(tmp_path / "test.db"))
        monkeypatch.setattr(ts_module, "_team_sessions", {})
        monkeypatch.setattr(ts_module, "_team_store", store)

        from fast_agent.spawn.team_spawner import get_team_session
        assert get_team_session("nonexistent") is None

    def test_corrupt_record_deleted_returns_none(self, tmp_path, monkeypatch):
        import fast_agent.spawn.team_spawner as ts_module
        from fast_agent.spawn.registry_backends import TeamSessionStore

        store = TeamSessionStore(str(tmp_path / "test.db"))
        corrupt = {"session_id": "ses-bad", "template": {}, "workspace": "/tmp",
                   "agents": {}, "sprint_status": "pending"}
        store.upsert("ses-bad", corrupt)

        monkeypatch.setattr(ts_module, "_team_sessions", {})
        monkeypatch.setattr(ts_module, "_team_store", store)

        from fast_agent.spawn.team_spawner import get_team_session
        result = get_team_session("ses-bad")

        assert result is None
        assert store.get("ses-bad") is None


# ── list_team_sessions ────────────────────────────────────────────────────────


class TestListTeamSessions:
    def test_returns_sessions_from_store(self, tmp_path, monkeypatch):
        import fast_agent.spawn.team_spawner as ts_module
        from fast_agent.spawn.registry_backends import TeamSessionStore

        store = TeamSessionStore(str(tmp_path / "test.db"))
        store.upsert("s1", _minimal_session_dict(session_id="s1"))
        store.upsert("s2", _minimal_session_dict(session_id="s2"))

        monkeypatch.setattr(ts_module, "_team_sessions", {})
        monkeypatch.setattr(ts_module, "_team_store", store)

        from fast_agent.spawn.team_spawner import list_team_sessions
        result = list_team_sessions()
        ids = {r["session_id"] for r in result}
        assert {"s1", "s2"}.issubset(ids)

    def test_in_memory_overrides_store(self, tmp_path, monkeypatch):
        """In-memory session (with unsaved state) beats stale DB record."""
        import fast_agent.spawn.team_spawner as ts_module
        from fast_agent.spawn.registry_backends import TeamSessionStore
        from fast_agent.spawn.team_spawner import TeamSession

        store = TeamSessionStore(str(tmp_path / "test.db"))
        store.upsert("s1", _minimal_session_dict(session_id="s1", sprint_status="pending"))

        live_session = TeamSession("s1", {"name": "sprint"}, Path("/tmp/ws"), "brief")
        live_session.sprint_status = "orchestrator_running"

        monkeypatch.setattr(ts_module, "_team_sessions", {"s1": live_session})
        monkeypatch.setattr(ts_module, "_team_store", store)

        from fast_agent.spawn.team_spawner import list_team_sessions
        result = list_team_sessions()
        s1 = next(r for r in result if r["session_id"] == "s1")
        assert s1["sprint_status"] == "orchestrator_running"

    def test_returns_empty_when_nothing_stored(self, tmp_path, monkeypatch):
        import fast_agent.spawn.team_spawner as ts_module
        from fast_agent.spawn.registry_backends import TeamSessionStore

        store = TeamSessionStore(str(tmp_path / "test.db"))
        monkeypatch.setattr(ts_module, "_team_sessions", {})
        monkeypatch.setattr(ts_module, "_team_store", store)

        from fast_agent.spawn.team_spawner import list_team_sessions
        assert list_team_sessions() == []


# ── spawn_team_members_for_session: fail loud ─────────────────────────────────


class TestSpawnTeamMembersFailLoud:
    @pytest.mark.asyncio
    async def test_raises_when_no_brief_and_no_first_task(self, tmp_path, monkeypatch):
        """ValueError raised when session has empty project_brief and no first_task."""
        import fast_agent.spawn.team_spawner as ts_module
        from fast_agent.spawn.team_spawner import TeamSession

        template = {
            "name": "sprint",
            "roles": {
                "dev": {"instruction": "You are a dev.", "servers": []},
            },
        }
        # Session with empty brief
        session = TeamSession("ses-fail", template, Path(tmp_path), project_brief="")
        session.agents = {
            "Minh [Dev]": {"run_id": "run-1", "role": "dev", "status": "available"},
        }

        monkeypatch.setattr(ts_module, "_team_sessions", {"ses-fail": session})

        from fast_agent.spawn.registry_backends import TeamSessionStore
        store = TeamSessionStore(str(tmp_path / "test.db"))
        monkeypatch.setattr(ts_module, "_team_store", store)

        registry_mock = MagicMock()
        registry_mock.list_active.return_value = []

        from fast_agent.spawn.team_spawner import spawn_team_members_for_session
        with pytest.raises(ValueError, match="project_brief"):
            await spawn_team_members_for_session(
                session_id="ses-fail",
                roles=["dev"],
                registry=registry_mock,
                first_task="",
            )

    @pytest.mark.asyncio
    async def test_no_error_when_first_task_provided(self, tmp_path, monkeypatch):
        """No ValueError when project_brief is empty but first_task is given."""
        import fast_agent.spawn.team_spawner as ts_module
        from fast_agent.spawn.team_spawner import TeamSession

        template = {
            "name": "sprint",
            "roles": {
                "dev": {"instruction": "You are a dev.", "servers": []},
            },
        }
        session = TeamSession("ses-ok", template, Path(tmp_path), project_brief="")
        session.agents = {
            "Minh [Dev]": {"run_id": "run-1", "role": "dev", "status": "available"},
        }

        from fast_agent.spawn.registry_backends import TeamSessionStore
        store = TeamSessionStore(str(tmp_path / "test.db"))
        store.upsert("ses-ok", session.to_dict())
        monkeypatch.setattr(ts_module, "_team_sessions", {"ses-ok": session})
        monkeypatch.setattr(ts_module, "_team_store", store)

        registry_mock = MagicMock()
        registry_mock.list_active.return_value = []

        with patch("fast_agent.spawn.team_spawner.run_isolated_agent_background") as mock_spawn:
            mock_spawn.return_value = "run-new-1"
            from fast_agent.spawn.team_spawner import spawn_team_members_for_session
            results = await spawn_team_members_for_session(
                session_id="ses-ok",
                roles=["dev"],
                registry=registry_mock,
                first_task="Write unit tests for auth module",
            )

        assert "dev" in results
        assert results["dev"]["status"] == "running"

    @pytest.mark.asyncio
    async def test_no_error_when_project_brief_provided(self, tmp_path, monkeypatch):
        """No ValueError when project_brief is set (even without first_task)."""
        import fast_agent.spawn.team_spawner as ts_module
        from fast_agent.spawn.team_spawner import TeamSession

        template = {
            "name": "sprint",
            "roles": {
                "dev": {"instruction": "You are a dev.", "servers": []},
            },
        }
        session = TeamSession("ses-brief", template, Path(tmp_path), project_brief="Build the API")
        session.agents = {
            "Minh [Dev]": {"run_id": "run-1", "role": "dev", "status": "available"},
        }

        from fast_agent.spawn.registry_backends import TeamSessionStore
        store = TeamSessionStore(str(tmp_path / "test.db"))
        store.upsert("ses-brief", session.to_dict())
        monkeypatch.setattr(ts_module, "_team_sessions", {"ses-brief": session})
        monkeypatch.setattr(ts_module, "_team_store", store)

        registry_mock = MagicMock()
        registry_mock.list_active.return_value = []

        with patch("fast_agent.spawn.team_spawner.run_isolated_agent_background") as mock_spawn:
            mock_spawn.return_value = "run-new-2"
            from fast_agent.spawn.team_spawner import spawn_team_members_for_session
            results = await spawn_team_members_for_session(
                session_id="ses-brief",
                roles=["dev"],
                registry=registry_mock,
                first_task="",
            )

        assert results["dev"]["status"] == "running"

    @pytest.mark.asyncio
    async def test_raises_when_session_not_found(self, tmp_path, monkeypatch):
        import fast_agent.spawn.team_spawner as ts_module
        from fast_agent.spawn.registry_backends import TeamSessionStore

        store = TeamSessionStore(str(tmp_path / "test.db"))
        monkeypatch.setattr(ts_module, "_team_sessions", {})
        monkeypatch.setattr(ts_module, "_team_store", store)

        registry_mock = MagicMock()
        from fast_agent.spawn.team_spawner import spawn_team_members_for_session
        with pytest.raises(ValueError, match="not found"):
            await spawn_team_members_for_session(
                session_id="no-such-session",
                roles=["dev"],
                registry=registry_mock,
            )


# ── SpawnProgressBridge: team event routing ───────────────────────────────────


class TestSpawnProgressBridgeTeamEvents:
    def _make_bridge(self):
        from services.spawn_progress_bridge import SpawnProgressBridge
        pm = MagicMock()
        bridge = SpawnProgressBridge(pm)
        return bridge, pm

    def test_team_spawned_does_not_push_to_chat_sse(self):
        """team_spawned events go to activity stream only — not chat SSE."""
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-123")  # active request

        with patch("services.activity_stream.activity_stream_manager.broadcast") as broadcast:
            line = json.dumps({
                "agent_name": "system",
                "event_type": "team_spawned",
                "run_id": "",
                "data": {"session_id": "ses-001", "team_name": "sprint"},
            })
            bridge._process_event_line(line)

        pm.push.assert_not_called()  # MUST NOT push to chat SSE
        assert broadcast.called  # broadcast called at least once (step 3 + step 5d)

    def test_team_member_spawned_does_not_push_to_chat_sse(self):
        """team_member_spawned events go to activity stream only — not chat SSE."""
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-456")

        with patch("services.activity_stream.activity_stream_manager.broadcast") as broadcast:
            line = json.dumps({
                "agent_name": "system",
                "event_type": "team_member_spawned",
                "run_id": "",
                "data": {"session_id": "ses-001", "team_name": "sprint", "role": "dev"},
            })
            bridge._process_event_line(line)

        pm.push.assert_not_called()
        assert broadcast.called

    def test_team_events_skipped_even_without_request_id(self):
        """team_ events exit early before chat SSE check, regardless of request_id."""
        bridge, pm = self._make_bridge()
        # No request_id set

        with patch("services.activity_stream.activity_stream_manager.broadcast"):
            line = json.dumps({
                "agent_name": "system",
                "event_type": "team_spawned",
                "run_id": "",
                "data": {"session_id": "ses-001", "team_name": "sprint"},
            })
            bridge._process_event_line(line)

        pm.push.assert_not_called()

    def test_unknown_team_prefixed_event_also_skipped(self):
        """Any event starting with 'team_' is broadcast-only."""
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-789")

        with patch("services.activity_stream.activity_stream_manager.broadcast"):
            line = json.dumps({
                "agent_name": "system",
                "event_type": "team_status_update",
                "run_id": "",
                "data": {},
            })
            bridge._process_event_line(line)

        pm.push.assert_not_called()

    def test_non_team_event_still_pushes_to_chat_sse(self):
        """Regular events (e.g. 'started') still reach chat SSE when request_id set."""
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-abc")

        line = json.dumps({
            "agent_name": "Minh [Dev]",
            "event_type": "started",
            "run_id": "run-1",
            "data": {"model": "gpt-4o", "servers": []},
        })
        bridge._process_event_line(line)

        pm.push.assert_called_once()
        assert pm.push.call_args[0][1] == "spawn_started"


# ── AgentRegistryDB: team_sessions queries ───────────────────────────────────


class TestAgentRegistryDBTeamSessions:
    def _setup_db_with_table(self, tmp_path):
        """DB with team_sessions table pre-created (simulates existing deployment)."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE team_sessions (session_id TEXT PRIMARY KEY, data_json TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        return db_path

    def _setup_db_fresh(self, tmp_path):
        """Fresh DB with NO team_sessions table (simulates first deployment)."""
        db_path = str(tmp_path / "fresh.db")
        # Just create an empty SQLite file — no tables
        conn = sqlite3.connect(db_path)
        conn.close()
        return db_path

    def test_get_team_session_returns_record(self, tmp_path, monkeypatch):
        db_path = self._setup_db_with_table(tmp_path)
        data = _minimal_session_dict(session_id="ses-001")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO team_sessions VALUES (?, ?)",
            ("ses-001", json.dumps(data)),
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)
        from core.agent_registry_db import AgentRegistryDB
        db = AgentRegistryDB()
        result = db.get_team_session("ses-001")
        assert result is not None
        assert result["session_id"] == "ses-001"
        assert result["project_brief"] == "Build a REST API"

    def test_get_team_session_returns_none_when_missing(self, tmp_path, monkeypatch):
        db_path = self._setup_db_with_table(tmp_path)
        monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)
        from core.agent_registry_db import AgentRegistryDB
        db = AgentRegistryDB()
        assert db.get_team_session("nonexistent") is None

    def test_get_team_session_creates_table_on_fresh_db(self, tmp_path, monkeypatch):
        """Fresh DB with no team_sessions table → auto-migration, returns None (not found)."""
        db_path = self._setup_db_fresh(tmp_path)
        monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)
        from core.agent_registry_db import AgentRegistryDB
        db = AgentRegistryDB()
        result = db.get_team_session("any-id")
        assert result is None  # not found, but no OperationalError

    def test_list_team_sessions_returns_all(self, tmp_path, monkeypatch):
        db_path = self._setup_db_with_table(tmp_path)
        conn = sqlite3.connect(db_path)
        for i in range(3):
            d = _minimal_session_dict(session_id=f"ses-00{i}")
            conn.execute("INSERT INTO team_sessions VALUES (?, ?)", (f"ses-00{i}", json.dumps(d)))
        conn.commit()
        conn.close()

        monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)
        from core.agent_registry_db import AgentRegistryDB
        db = AgentRegistryDB()
        results = db.list_team_sessions()
        assert len(results) == 3

    def test_list_team_sessions_empty(self, tmp_path, monkeypatch):
        db_path = self._setup_db_with_table(tmp_path)
        monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)
        from core.agent_registry_db import AgentRegistryDB
        db = AgentRegistryDB()
        assert db.list_team_sessions() == []

    def test_list_team_sessions_creates_table_on_fresh_db(self, tmp_path, monkeypatch):
        """Fresh DB → auto-migration creates table, returns [] instead of raising."""
        db_path = self._setup_db_fresh(tmp_path)
        monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)
        from core.agent_registry_db import AgentRegistryDB
        db = AgentRegistryDB()
        assert db.list_team_sessions() == []

    def test_errors_propagate_not_swallowed(self, tmp_path, monkeypatch):
        """DB errors raise, not silently return None."""
        # Use a path whose parent directory doesn't exist — SQLite cannot create the file
        missing_dir_path = str(tmp_path / "no_such_dir" / "nested.db")
        monkeypatch.setenv("SPAWN_REGISTRY_DB", missing_dir_path)
        from core.agent_registry_db import AgentRegistryDB
        with pytest.raises(Exception):
            AgentRegistryDB()  # __init__ calls _ensure_team_sessions_table → raises
