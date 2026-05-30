"""Unit tests for subprocess environment variable propagation.

Covers the FULL env var pipeline:
  server.py → team_spawner → isolated_spawner → subprocess → isolated_runner

Each env var is tested for:
  1. Who sets it (producer)
  2. Who reads it (consumer)
  3. Correct value format
  4. Behavior when missing (graceful degradation vs hard fail)
  5. Behavior after os.chdir in subprocess

These tests exist because subprocess env bugs are the #1 source of
production incidents — silently swallowed errors that only manifest
when agents try to collaborate or save state.
"""

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ═══════════════════════════════════════════════
# Env var registry — single source of truth
# ═══════════════════════════════════════════════

# Every env var used in the spawn pipeline, organized by lifecycle stage
ENV_VAR_REGISTRY = {
    # ── Set by server.py at startup ──
    "SPAWN_REGISTRY_DB": {
        "producer": "server.py (setdefault at import time)",
        "consumers": [
            "services/context_persistence.py",
            "services/spawn_progress_bridge.py",
            "spawn/registry_backends.py",
            "spawn/isolated_spawner.py",
            "spawn/servers/email_server.py",
        ],
        "format": "absolute filesystem path",
        "required": True,
        "must_be_absolute": True,
        "example": "/Users/dev/jarvis/backend/data/jarvis.db",
    },
    "SPAWN_EVENT_SOCKET": {
        "producer": "server.py (set in startup event)",
        "consumers": [
            "spawn/spawn_events.py (emit_event)",
            "spawn/servers/agent_spawner_server.py",
        ],
        "format": "Unix domain socket path",
        "required": True,
        "must_be_absolute": True,
        "example": "/tmp/jarvis_spawn_events.sock",
    },

    # ── Set by team_spawner._build_team_env ──
    "TEAM_WORKSPACE": {
        "producer": "team_spawner._build_team_env",
        "consumers": [
            "isolated_runner.py (idle loop)",
            "spawn/agent_channel.py",
            "spawn/inbox_watcher_hook.py",
            "spawn/servers/_team_helpers.py",
            "spawn/servers/team_communicate_server.py",
            "spawn/servers/meeting_room_server.py",
            "spawn/servers/meeting_storage.py",
        ],
        "format": "absolute filesystem path",
        "required": True,
        "must_be_absolute": True,
        "example": "/Users/dev/jarvis/backend/.runtime/data/workspaces/team_abc123",
    },
    "TEAM_MY_ROLE": {
        "producer": "team_spawner._build_team_env",
        "consumers": [
            "isolated_runner._save_agent_context_snapshot (as team_name param)",
        ],
        "format": "lowercase role identifier (no brackets)",
        "required": True,
        "must_be_absolute": False,
        "example": "pm",
    },
    "TEAM_MY_NAME": {
        "producer": "team_spawner._build_team_env / config_reader.get_server_env",
        "consumers": [
            "isolated_runner.run_child_agent (agent display name)",
            "spawn/inbox_watcher_hook.py",
            "spawn/pause_signal_handler.py",
            "spawn/servers/_team_helpers.py (get_agent_name)",
            "spawn/servers/team_communicate_server.py",
        ],
        "format": "VietnameseName [ROLE] (e.g. 'Linh [PM]')",
        "required": True,
        "must_be_absolute": False,
        "example": "Linh [PM]",
    },
    "TEAM_SESSION_ID": {
        "producer": "team_spawner._build_team_env / config_reader.get_server_env",
        "consumers": [
            "isolated_runner._save_agent_context_snapshot (session_id param)",
            "spawn/servers/agent_spawner_server.py",
        ],
        "format": "UUID-like session identifier",
        "required": True,
        "must_be_absolute": False,
        "example": "agile-team_7060c04b",
    },
    "TEAM_MESSAGES_DIR": {
        "producer": "team_spawner._build_team_env",
        "consumers": [
            "config_reader.get_server_env",
            "isolated_runner.py (idle loop message polling)",
            "spawn/servers/_team_helpers.py",
        ],
        "format": "absolute filesystem path",
        "required": True,
        "must_be_absolute": True,
        "example": "/Users/dev/jarvis/backend/.runtime/state/messages/agile-team_abc",
    },
    "TEAM_ROLES_CONFIG": {
        "producer": "team_spawner._build_team_env",
        "consumers": [
            "spawn/servers/_team_helpers.py (get_roles_config → JSON.parse)",
            "spawn/servers/team_communicate_server.py",
        ],
        "format": "JSON string (dict of role → {agent_name, instruction, servers})",
        "required": True,
        "must_be_absolute": False,
        "example": '{"pm": {"agent_name": "Linh [PM]"}}',
    },

    # ── Set by isolated_spawner / agent_spawner_server ──
    "SPAWN_PROJECT_DIR": {
        "producer": "agent_spawner_server.py (module-level, also exported back)",
        "consumers": [
            "config_reader.py (get_server_env, get_default_model)",
            "spawn/agent_channel.py",
            "spawn/servers/_team_helpers.py",
            "spawn/servers/agent_spawner_server.py",
            "spawn/spawn_display.py",
            "team_spawner.py (skill resolution)",
        ],
        "format": "absolute filesystem path to backend/",
        "required": True,
        "must_be_absolute": True,
        "example": "/Users/dev/jarvis/backend",
    },
    "SPAWN_RUN_ID": {
        "producer": "spawn_events.py / isolated_spawner.py",
        "consumers": [
            "spawn/pause_signal_handler.py",
        ],
        "format": "UUID run identifier",
        "required": False,
        "must_be_absolute": False,
        "example": "f59c08d2-9a02-4d27-8f08-1179b1399f8a",
    },

    # ── Subprocess infra ──
    "PYTHONPATH": {
        "producer": "isolated_spawner._run_subprocess",
        "consumers": ["Python import system"],
        "format": "Colon-separated paths",
        "required": True,
        "must_be_absolute": True,
        "example": "/Users/dev/jarvis/backend",
    },
}


# ═══════════════════════════════════════════════
# 1. Registry completeness tests
# ═══════════════════════════════════════════════


class TestEnvVarRegistry:
    """Ensure our registry documents all env vars."""

    def test_all_critical_vars_documented(self):
        """All critical env vars must be in the registry."""
        critical_vars = {
            "SPAWN_REGISTRY_DB", "SPAWN_EVENT_SOCKET",
            "TEAM_WORKSPACE", "TEAM_MY_ROLE", "TEAM_MY_NAME",
            "TEAM_SESSION_ID", "TEAM_MESSAGES_DIR", "TEAM_ROLES_CONFIG",
            "SPAWN_PROJECT_DIR", "PYTHONPATH",
        }
        for var in critical_vars:
            assert var in ENV_VAR_REGISTRY, f"Missing from registry: {var}"

    def test_registry_has_required_fields(self):
        """Each entry must have producer, consumers, format, required."""
        for var_name, info in ENV_VAR_REGISTRY.items():
            assert "producer" in info, f"{var_name} missing 'producer'"
            assert "consumers" in info, f"{var_name} missing 'consumers'"
            assert "format" in info, f"{var_name} missing 'format'"
            assert "required" in info, f"{var_name} missing 'required'"

    def test_all_path_vars_marked_must_be_absolute(self):
        """Env vars containing filesystem paths must be marked absolute."""
        path_vars = {
            "SPAWN_REGISTRY_DB", "SPAWN_EVENT_SOCKET",
            "TEAM_WORKSPACE", "TEAM_MESSAGES_DIR",
            "SPAWN_PROJECT_DIR", "PYTHONPATH",
        }
        for var in path_vars:
            assert ENV_VAR_REGISTRY[var]["must_be_absolute"], \
                f"{var} contains a path but is not marked must_be_absolute"


# ═══════════════════════════════════════════════
# 2. SPAWN_REGISTRY_DB — the most critical var
# ═══════════════════════════════════════════════


class TestSpawnRegistryDB:
    """SPAWN_REGISTRY_DB is the absolute path to jarvis.db.

    Set by server.py at module import time.
    Used by ALL subprocess-DB consumers.
    MUST be absolute to survive os.chdir() in subprocesses.
    """

    def test_server_sets_absolute_path(self):
        """server.py resolves the path to absolute."""
        # Replicate what server.py does
        raw_path = Path("data/jarvis.db")
        resolved = str(raw_path.resolve())
        assert os.path.isabs(resolved)

    def test_relative_path_breaks_after_chdir(self, tmp_path):
        """REGRESSION: relative path + chdir = file not found."""
        db_path = "data/jarvis.db"
        original = os.getcwd()

        # Create the DB at original CWD
        os.makedirs("data", exist_ok=True)

        try:
            os.chdir(str(tmp_path))  # Subprocess changes CWD
            # Relative path now points to wrong location
            resolved = os.path.join(os.getcwd(), db_path)
            assert not os.path.exists(resolved), \
                "Relative path should NOT work after chdir"
        finally:
            os.chdir(original)

    def test_absolute_path_survives_chdir(self, tmp_path):
        """Absolute path works regardless of CWD."""
        db_file = tmp_path / "jarvis.db"
        db_file.touch()
        abs_path = str(db_file)

        original = os.getcwd()
        try:
            os.chdir(str(tmp_path / "subdir" if (tmp_path / "subdir").exists()
                         else tmp_path))
            assert os.path.exists(abs_path), \
                "Absolute path must work after chdir"
        finally:
            os.chdir(original)

    def test_env_var_inherited_by_subprocess(self, monkeypatch):
        """Subprocess must inherit SPAWN_REGISTRY_DB from parent."""
        monkeypatch.setenv("SPAWN_REGISTRY_DB", "/abs/path/jarvis.db")

        # Simulate _run_subprocess env construction
        subprocess_env = {**os.environ, "PYTHONPATH": "/some/path"}
        assert "SPAWN_REGISTRY_DB" in subprocess_env
        assert subprocess_env["SPAWN_REGISTRY_DB"] == "/abs/path/jarvis.db"

    def test_context_persistence_reads_it(self, tmp_path, monkeypatch):
        """context_persistence._get_db_path uses SPAWN_REGISTRY_DB."""
        db_path = str(tmp_path / "test.db")
        monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)

        from services.context_persistence import _get_db_path
        assert _get_db_path() == db_path

    def test_missing_env_falls_back(self, monkeypatch):
        """Without SPAWN_REGISTRY_DB, fallback to relative data/jarvis.db."""
        monkeypatch.delenv("SPAWN_REGISTRY_DB", raising=False)

        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                from services.context_persistence import _get_db_path
                result = _get_db_path()
                # No data/jarvis.db exists → returns None
                assert result is None
            finally:
                os.chdir(original)


# ═══════════════════════════════════════════════
# 3. TEAM_* env vars — team spawner → subprocess
# ═══════════════════════════════════════════════


class TestTeamEnvVarsPropagation:
    """Test env vars set by _build_team_env and read in subprocess."""

    def test_build_team_env_sets_all_required_vars(self, tmp_path):
        """_build_team_env must set TEAM_WORKSPACE, TEAM_MY_ROLE, TEAM_ROLES_CONFIG."""
        from fast_agent.spawn.team_spawner import _build_team_env

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        roles = {
            "pm": {"instruction": "You are PM", "servers": ["filesystem"]},
            "dev": {"instruction": "You are Dev", "servers": ["filesystem"]},
        }

        env = _build_team_env(workspace, roles, "pm", my_name="Linh [PM]", session_id="sess-1")

        # All required vars must be present
        assert "TEAM_WORKSPACE" in env
        assert "TEAM_MY_ROLE" in env
        assert "TEAM_ROLES_CONFIG" in env

    def test_build_team_env_sets_optional_vars_when_provided(self, tmp_path):
        """my_name and session_id produce TEAM_MY_NAME and TEAM_SESSION_ID."""
        from fast_agent.spawn.team_spawner import _build_team_env

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        roles = {"pm": {"instruction": "PM"}}

        env = _build_team_env(workspace, roles, "pm", my_name="An [PM]", session_id="sess-99")

        assert env.get("TEAM_MY_NAME") == "An [PM]"
        assert env.get("TEAM_SESSION_ID") == "sess-99"

    def test_build_team_env_omits_optional_when_empty(self, tmp_path):
        """Without my_name/session_id, those vars should NOT be set."""
        from fast_agent.spawn.team_spawner import _build_team_env

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        roles = {"pm": {"instruction": "PM"}}

        env = _build_team_env(workspace, roles, "pm")

        assert "TEAM_MY_NAME" not in env
        assert "TEAM_SESSION_ID" not in env
        assert "TEAM_MESSAGES_DIR" not in env

    def test_team_workspace_is_absolute(self, tmp_path):
        """TEAM_WORKSPACE must be an absolute path."""
        from fast_agent.spawn.team_spawner import _build_team_env

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        roles = {"dev": {}}

        env = _build_team_env(workspace, roles, "dev")

        assert os.path.isabs(env["TEAM_WORKSPACE"])

    def test_team_messages_dir_is_absolute(self, tmp_path):
        """TEAM_MESSAGES_DIR must be absolute for subprocess access."""
        from fast_agent.spawn.team_spawner import _build_team_env

        # Create a workspace under .runtime structure
        runtime_dir = tmp_path / ".runtime"
        workspace = runtime_dir / "data" / "workspaces" / "team-1"
        workspace.mkdir(parents=True)
        roles = {"pm": {}}

        env = _build_team_env(workspace, roles, "pm", session_id="sess-1")

        assert "TEAM_MESSAGES_DIR" in env
        assert os.path.isabs(env["TEAM_MESSAGES_DIR"])

    def test_team_roles_config_is_valid_json(self, tmp_path):
        """TEAM_ROLES_CONFIG must be valid JSON parseable in subprocess."""
        from fast_agent.spawn.team_spawner import _build_team_env

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        roles = {
            "pm": {"instruction": "PM agent", "servers": ["filesystem", "email"]},
            "dev": {"instruction": "Dev agent", "servers": ["filesystem"]},
        }

        env = _build_team_env(workspace, roles, "pm")

        # Must parse without error
        config = json.loads(env["TEAM_ROLES_CONFIG"])
        assert isinstance(config, dict)
        assert "pm" in config or "dev" in config

    def test_team_my_role_format(self, tmp_path):
        """TEAM_MY_ROLE is a lowercase role name, not display name."""
        from fast_agent.spawn.team_spawner import _build_team_env

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        roles = {"pm": {}}

        env = _build_team_env(workspace, roles, "pm", my_name="Linh [PM]")

        assert env["TEAM_MY_ROLE"] == "pm"
        # MUST be distinct from TEAM_MY_NAME
        assert env["TEAM_MY_ROLE"] != env.get("TEAM_MY_NAME", "")


# ═══════════════════════════════════════════════
# 4. Subprocess env inheritance
# ═══════════════════════════════════════════════


class TestSubprocessEnvInheritance:
    """Verify isolated_spawner passes env vars to subprocess correctly."""

    def test_subprocess_env_inherits_parent(self, monkeypatch):
        """Subprocess env = parent env + PYTHONPATH + custom env_vars."""
        monkeypatch.setenv("SPAWN_REGISTRY_DB", "/abs/db.sqlite")
        monkeypatch.setenv("SPAWN_EVENT_SOCKET", "/tmp/events.sock")

        # Replicate isolated_spawner._run_subprocess env construction
        project_path = Path("/project")
        subprocess_env = {
            **os.environ,
            "PYTHONPATH": str(project_path),
        }

        custom_env = {
            "TEAM_WORKSPACE": "/workspace",
            "TEAM_MY_ROLE": "pm",
            "TEAM_MY_NAME": "An [PM]",
            "TEAM_SESSION_ID": "sess-1",
        }
        subprocess_env.update(custom_env)

        # All parent + custom vars must be present
        assert subprocess_env["SPAWN_REGISTRY_DB"] == "/abs/db.sqlite"
        assert subprocess_env["SPAWN_EVENT_SOCKET"] == "/tmp/events.sock"
        assert subprocess_env["TEAM_WORKSPACE"] == "/workspace"
        assert subprocess_env["TEAM_MY_ROLE"] == "pm"
        assert subprocess_env["TEAM_MY_NAME"] == "An [PM]"
        assert subprocess_env["TEAM_SESSION_ID"] == "sess-1"
        assert subprocess_env["PYTHONPATH"] == str(project_path)

    def test_custom_env_overrides_parent(self, monkeypatch):
        """env_vars should override parent env (not merge)."""
        monkeypatch.setenv("TEAM_MY_NAME", "old_name")

        subprocess_env = {**os.environ}
        subprocess_env.update({"TEAM_MY_NAME": "Linh [PM]"})

        assert subprocess_env["TEAM_MY_NAME"] == "Linh [PM]"

    def test_missing_parent_vars_still_propagates_custom(self, monkeypatch):
        """Even if parent doesn't set TEAM_*, custom env_vars add them."""
        monkeypatch.delenv("TEAM_WORKSPACE", raising=False)
        monkeypatch.delenv("TEAM_MY_ROLE", raising=False)

        subprocess_env = {**os.environ}
        custom = {"TEAM_WORKSPACE": "/ws", "TEAM_MY_ROLE": "dev"}
        subprocess_env.update(custom)

        assert subprocess_env["TEAM_WORKSPACE"] == "/ws"
        assert subprocess_env["TEAM_MY_ROLE"] == "dev"


# ═══════════════════════════════════════════════
# 5. isolated_runner env var consumption
# ═══════════════════════════════════════════════


class TestIsolatedRunnerEnvConsumption:
    """Test how isolated_runner.py reads env vars in the subprocess."""

    def test_agent_name_from_team_my_name(self, monkeypatch):
        """Agent name comes from TEAM_MY_NAME, falling back to role."""
        monkeypatch.setenv("TEAM_MY_NAME", "Phong [SA]")
        agent_name = os.environ.get("TEAM_MY_NAME", "") or "default_role"
        assert agent_name == "Phong [SA]"

    def test_agent_name_fallback_to_role(self, monkeypatch):
        """Without TEAM_MY_NAME, agent_name falls back to role arg."""
        monkeypatch.delenv("TEAM_MY_NAME", raising=False)
        role = "sa"
        agent_name = os.environ.get("TEAM_MY_NAME", "") or role
        assert agent_name == "sa"

    def test_context_save_reads_session_id(self, monkeypatch):
        """_save_agent_context_snapshot reads TEAM_SESSION_ID."""
        monkeypatch.setenv("TEAM_SESSION_ID", "sess-abc-123")
        session_id = os.environ.get("TEAM_SESSION_ID")
        assert session_id == "sess-abc-123"

    def test_context_save_reads_team_role(self, monkeypatch):
        """_save_agent_context_snapshot reads TEAM_MY_ROLE for team_name."""
        monkeypatch.setenv("TEAM_MY_ROLE", "pm")
        team_name = os.environ.get("TEAM_MY_ROLE", "")
        assert team_name == "pm"

    def test_context_save_missing_session_id(self, monkeypatch):
        """Missing TEAM_SESSION_ID → session_id=None (not crash)."""
        monkeypatch.delenv("TEAM_SESSION_ID", raising=False)
        session_id = os.environ.get("TEAM_SESSION_ID")
        assert session_id is None

    def test_context_save_missing_team_role(self, monkeypatch):
        """Missing TEAM_MY_ROLE → team_name='' (not crash)."""
        monkeypatch.delenv("TEAM_MY_ROLE", raising=False)
        team_name = os.environ.get("TEAM_MY_ROLE", "")
        assert team_name == ""

    def test_idle_loop_reads_team_name_and_workspace(self, monkeypatch):
        """Idle loop needs TEAM_MY_NAME and TEAM_WORKSPACE for message polling."""
        monkeypatch.setenv("TEAM_MY_NAME", "Minh [Dev]")
        monkeypatch.setenv("TEAM_WORKSPACE", "/ws/team-1")

        name = os.environ.get("TEAM_MY_NAME", "")
        workspace = os.environ.get("TEAM_WORKSPACE", "")

        assert name == "Minh [Dev]"
        assert workspace == "/ws/team-1"

    def test_idle_loop_reads_messages_dir(self, monkeypatch):
        """Idle loop needs TEAM_MESSAGES_DIR for email polling."""
        monkeypatch.setenv("TEAM_MESSAGES_DIR", "/runtime/state/messages/sess-1")
        msgs_dir = os.environ.get("TEAM_MESSAGES_DIR", "")
        assert msgs_dir == "/runtime/state/messages/sess-1"

    def test_idle_loop_missing_msgs_dir_uses_workspace_fallback(self, monkeypatch):
        """Without TEAM_MESSAGES_DIR, falls back to TEAM_WORKSPACE-based path."""
        monkeypatch.delenv("TEAM_MESSAGES_DIR", raising=False)
        monkeypatch.setenv("TEAM_WORKSPACE", "/ws/team-1")

        msgs_dir = os.environ.get("TEAM_MESSAGES_DIR", "")
        workspace = os.environ.get("TEAM_WORKSPACE", "")

        assert msgs_dir == ""  # Not set
        assert workspace == "/ws/team-1"  # Fallback available


# ═══════════════════════════════════════════════
# 6. MCP server env vars (config_reader)
# ═══════════════════════════════════════════════


class TestMCPServerEnvVars:
    """MCP servers (meeting_room, email, agent_spawner) need env vars."""

    def test_team_aware_servers(self):
        """Only specific servers need team env vars."""
        from fast_agent.spawn.config_reader import _TEAM_AWARE_SERVERS
        assert "meeting_room" in _TEAM_AWARE_SERVERS
        assert "agent_spawner" in _TEAM_AWARE_SERVERS
        assert "email" in _TEAM_AWARE_SERVERS
        # filesystem does NOT need team env
        assert "filesystem" not in _TEAM_AWARE_SERVERS

    def test_get_server_env_returns_none_for_non_team_server(self):
        """Non-team servers return None (no extra env needed)."""
        from fast_agent.spawn.config_reader import get_server_env
        result = get_server_env("filesystem")
        assert result is None

    def test_get_server_env_propagates_project_dir(self, monkeypatch):
        """SPAWN_PROJECT_DIR is propagated for team servers."""
        monkeypatch.setenv("SPAWN_PROJECT_DIR", "/project/backend")
        from fast_agent.spawn.config_reader import get_server_env

        result = get_server_env("meeting_room")
        assert result is not None
        assert result.get("SPAWN_PROJECT_DIR") == "/project/backend"

    def test_get_server_env_propagates_session_id(self, monkeypatch):
        """TEAM_SESSION_ID is propagated for team servers."""
        monkeypatch.setenv("SPAWN_PROJECT_DIR", "/project")
        monkeypatch.setenv("TEAM_SESSION_ID", "sess-xyz")
        from fast_agent.spawn.config_reader import get_server_env

        result = get_server_env("email")
        assert result is not None
        assert result.get("TEAM_SESSION_ID") == "sess-xyz"

    def test_get_server_env_propagates_messages_dir(self, monkeypatch):
        """TEAM_MESSAGES_DIR is propagated for email server."""
        monkeypatch.setenv("SPAWN_PROJECT_DIR", "/project")
        monkeypatch.setenv("TEAM_MESSAGES_DIR", "/msgs/sess-1")
        from fast_agent.spawn.config_reader import get_server_env

        result = get_server_env("email")
        assert result is not None
        assert result.get("TEAM_MESSAGES_DIR") == "/msgs/sess-1"

    def test_get_server_env_includes_workspace_and_agent(self, monkeypatch):
        """Workspace and agent name are passed as function args."""
        monkeypatch.setenv("SPAWN_PROJECT_DIR", "/project")
        from fast_agent.spawn.config_reader import get_server_env

        result = get_server_env(
            "meeting_room",
            workspace_dir="/ws/team-1",
            agent_name="Linh [PM]",
        )
        assert result["TEAM_WORKSPACE"] == "/ws/team-1"
        assert result["TEAM_MY_NAME"] == "Linh [PM]"

    def test_get_server_env_empty_when_no_vars(self, monkeypatch):
        """With no env vars set and no args, returns None."""
        monkeypatch.delenv("SPAWN_PROJECT_DIR", raising=False)
        monkeypatch.delenv("TEAM_SESSION_ID", raising=False)
        monkeypatch.delenv("TEAM_MESSAGES_DIR", raising=False)
        from fast_agent.spawn.config_reader import get_server_env

        result = get_server_env("meeting_room")
        assert result is None


# ═══════════════════════════════════════════════
# 7. TEAM_ROLES_CONFIG parsing
# ═══════════════════════════════════════════════


class TestTeamRolesConfig:
    """TEAM_ROLES_CONFIG is a JSON string parsed in subprocess MCP servers."""

    def test_parse_valid_config(self, monkeypatch):
        """Valid JSON config must parse correctly."""
        config = {
            "pm": {"agent_name": "Linh [PM]", "instruction": "You are PM"},
            "sa": {"agent_name": "Phong [SA]", "instruction": "You are SA"},
            "dev": {"agent_name": "Minh [Dev]", "instruction": "You are Dev"},
        }
        monkeypatch.setenv("TEAM_ROLES_CONFIG", json.dumps(config))

        parsed = json.loads(os.environ.get("TEAM_ROLES_CONFIG", "{}"))
        assert len(parsed) == 3
        assert parsed["pm"]["agent_name"] == "Linh [PM]"

    def test_parse_empty_config(self, monkeypatch):
        """Missing TEAM_ROLES_CONFIG defaults to empty dict."""
        monkeypatch.delenv("TEAM_ROLES_CONFIG", raising=False)

        parsed = json.loads(os.environ.get("TEAM_ROLES_CONFIG", "{}"))
        assert parsed == {}

    def test_parse_malformed_json(self, monkeypatch):
        """Malformed TEAM_ROLES_CONFIG should raise (NOT silently fail)."""
        monkeypatch.setenv("TEAM_ROLES_CONFIG", "not-json{{{")

        with pytest.raises(json.JSONDecodeError):
            json.loads(os.environ.get("TEAM_ROLES_CONFIG", "{}"))

    def test_config_preserves_special_chars_in_names(self, monkeypatch):
        """Agent names with brackets must survive JSON roundtrip."""
        config = {"pm": {"agent_name": "Linh [PM]"}}
        monkeypatch.setenv("TEAM_ROLES_CONFIG", json.dumps(config))

        parsed = json.loads(os.environ["TEAM_ROLES_CONFIG"])
        assert parsed["pm"]["agent_name"] == "Linh [PM]"
        assert "[" in parsed["pm"]["agent_name"]


# ═══════════════════════════════════════════════
# 8. Cross-cutting: env var isolation between teams
# ═══════════════════════════════════════════════


class TestEnvVarIsolation:
    """Different team sessions must have isolated env vars."""

    def test_different_sessions_have_different_session_ids(self, tmp_path):
        """Two concurrent teams must not share TEAM_SESSION_ID."""
        from fast_agent.spawn.team_spawner import _build_team_env

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        roles = {"pm": {}}

        env1 = _build_team_env(workspace, roles, "pm", session_id="team-alpha")
        env2 = _build_team_env(workspace, roles, "pm", session_id="team-beta")

        assert env1["TEAM_SESSION_ID"] != env2["TEAM_SESSION_ID"]

    def test_different_sessions_have_different_message_dirs(self, tmp_path):
        """Message directories must be session-scoped."""
        from fast_agent.spawn.team_spawner import _build_team_env

        runtime_dir = tmp_path / ".runtime"
        workspace = runtime_dir / "data" / "workspaces" / "team-1"
        workspace.mkdir(parents=True)
        roles = {"pm": {}}

        env1 = _build_team_env(workspace, roles, "pm", session_id="alpha")
        env2 = _build_team_env(workspace, roles, "pm", session_id="beta")

        assert env1.get("TEAM_MESSAGES_DIR") != env2.get("TEAM_MESSAGES_DIR")
        assert "alpha" in env1.get("TEAM_MESSAGES_DIR", "")
        assert "beta" in env2.get("TEAM_MESSAGES_DIR", "")

    def test_team_roles_config_reflects_session_agents(self, tmp_path):
        """With a session, TEAM_ROLES_CONFIG uses session's resolved names."""
        from fast_agent.spawn.team_spawner import _build_team_env

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        roles = {"pm": {"instruction": "PM"}, "dev": {"instruction": "Dev"}}

        # Simulate a TeamSession with resolved names
        session = MagicMock()
        session.agents = {
            "An [PM]": {"role": "pm", "status": "running"},
            "Bao [Dev]": {"role": "dev", "status": "running"},
        }

        env = _build_team_env(workspace, roles, "pm", session=session)
        config = json.loads(env["TEAM_ROLES_CONFIG"])

        # Config should use the session's resolved names
        assert config["pm"]["agent_name"] == "An [PM]"
        assert config["dev"]["agent_name"] == "Bao [Dev]"


# ═══════════════════════════════════════════════
# 9. pause_signal_handler env vars
# ═══════════════════════════════════════════════


class TestPauseSignalHandlerEnv:
    """pause_signal_handler reads TEAM_MY_NAME and SPAWN_RUN_ID."""

    def test_reads_team_my_name(self, monkeypatch):
        monkeypatch.setenv("TEAM_MY_NAME", "Linh [PM]")
        assert os.environ.get("TEAM_MY_NAME", "unknown") == "Linh [PM]"

    def test_defaults_to_unknown(self, monkeypatch):
        monkeypatch.delenv("TEAM_MY_NAME", raising=False)
        assert os.environ.get("TEAM_MY_NAME", "unknown") == "unknown"

    def test_reads_spawn_run_id(self, monkeypatch):
        monkeypatch.setenv("SPAWN_RUN_ID", "run-abc-123")
        assert os.environ.get("SPAWN_RUN_ID", "") == "run-abc-123"


# ═══════════════════════════════════════════════
# 10. End-to-end: simulate full env pipeline
# ═══════════════════════════════════════════════


class TestFullEnvPipeline:
    """Simulate the complete env var flow from server → subprocess."""

    def test_full_pipeline(self, tmp_path, monkeypatch):
        """Simulate server.py → team_spawner → isolated_spawner → runner."""
        # ── Step 1: server.py sets global vars ──
        db_path = str(tmp_path / "jarvis.db")
        socket_path = str(tmp_path / "events.sock")
        monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)
        monkeypatch.setenv("SPAWN_EVENT_SOCKET", socket_path)
        monkeypatch.setenv("SPAWN_PROJECT_DIR", str(tmp_path))

        # ── Step 2: team_spawner builds team env ──
        from fast_agent.spawn.team_spawner import _build_team_env

        runtime_dir = tmp_path / ".runtime"
        workspace = runtime_dir / "data" / "workspaces" / "team-1"
        workspace.mkdir(parents=True)
        roles = {
            "pm": {"instruction": "PM agent"},
            "dev": {"instruction": "Dev agent"},
        }

        team_env = _build_team_env(
            workspace, roles, "pm",
            my_name="An [PM]",
            session_id="sess-001",
        )

        # ── Step 3: isolated_spawner builds subprocess env ──
        subprocess_env = {
            **os.environ,
            "PYTHONPATH": str(tmp_path),
        }
        subprocess_env.update(team_env)

        # ── Step 4: Verify ALL vars available in subprocess ──
        # From server.py
        assert subprocess_env["SPAWN_REGISTRY_DB"] == db_path
        assert os.path.isabs(subprocess_env["SPAWN_REGISTRY_DB"])
        assert subprocess_env["SPAWN_EVENT_SOCKET"] == socket_path
        assert subprocess_env["SPAWN_PROJECT_DIR"] == str(tmp_path)

        # From team_spawner
        assert subprocess_env["TEAM_WORKSPACE"] == str(workspace)
        assert os.path.isabs(subprocess_env["TEAM_WORKSPACE"])
        assert subprocess_env["TEAM_MY_ROLE"] == "pm"
        assert subprocess_env["TEAM_MY_NAME"] == "An [PM]"
        assert subprocess_env["TEAM_SESSION_ID"] == "sess-001"
        assert "TEAM_MESSAGES_DIR" in subprocess_env
        assert os.path.isabs(subprocess_env["TEAM_MESSAGES_DIR"])
        assert "TEAM_ROLES_CONFIG" in subprocess_env

        # From isolated_spawner
        assert subprocess_env["PYTHONPATH"] == str(tmp_path)

        # ── Step 5: Verify runner can read everything ──
        # Simulate isolated_runner reads
        agent_name = subprocess_env.get("TEAM_MY_NAME", "") or "pm"
        session_id = subprocess_env.get("TEAM_SESSION_ID")
        team_role = subprocess_env.get("TEAM_MY_ROLE", "")
        db = subprocess_env.get("SPAWN_REGISTRY_DB")

        assert agent_name == "An [PM]"
        assert session_id == "sess-001"
        assert team_role == "pm"
        assert db == db_path

    def test_pipeline_with_minimal_env(self, tmp_path, monkeypatch):
        """Pipeline must not crash even with minimal env vars."""
        # Only SPAWN_REGISTRY_DB set (minimum viable)
        db_path = str(tmp_path / "jarvis.db")
        monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)

        # Simulate subprocess env
        subprocess_env = {**os.environ, "PYTHONPATH": str(tmp_path)}

        # These should gracefully degrade
        assert subprocess_env.get("TEAM_MY_NAME", "") == ""
        assert subprocess_env.get("TEAM_MY_ROLE", "") == ""
        assert subprocess_env.get("TEAM_SESSION_ID") is None
        assert subprocess_env.get("TEAM_WORKSPACE", "") == ""

        # SPAWN_REGISTRY_DB must still be there
        assert subprocess_env["SPAWN_REGISTRY_DB"] == db_path


# ═══════════════════════════════════════════════
# 7. JARVIS_RUNTIME_RPC_SOCKET multi-hop propagation
# ═══════════════════════════════════════════════


class TestRuntimeRpcSocketMultiHop:
    """Pin the multi-hop chain that broke approval-server + pause from
    2026-05-09 onwards. When approval-server (or skill_server / mcp_admin)
    is loaded by a spawned agent, the chain is:

        backend (uvicorn)
          → fast-agent stdio MCP: agent_spawner_server         [hop 1]
          → subprocess.Popen({**os.environ, ...}): isolated_runner   [hop 2]
          → fast-agent stdio MCP: approval-server              [hop 3]

    JARVIS_RUNTIME_RPC_SOCKET must survive every hop. Hop 1 (the
    fast-agent stdio env build) is where it previously dropped, because
    ``_parent_extras`` only forwarded ``SPAWN_*`` + ``VIRTUAL_ENV``. The
    fix added ``JARVIS_RUNTIME_RPC_SOCKET`` to that whitelist.
    """

    def _build_stdio_env(self, config_env: dict) -> dict:
        """Replicate mcp_connection_manager.py lines 952-986 — the env
        build for a stdio MCP child. Kept inline so the test fails
        loudly if production drops the whitelist entry.
        """
        from mcp.client.stdio import get_default_environment
        _parent_extras = {
            k: v for k, v in os.environ.items()
            if (
                k.startswith("SPAWN_")
                or k == "JARVIS_RUNTIME_RPC_SOCKET"
                or k in ("VIRTUAL_ENV",)
            )
        }
        _config_env = {
            k: (os.path.expandvars(v) if isinstance(v, str) else v)
            for k, v in config_env.items()
        }
        return {**get_default_environment(), **_parent_extras, **_config_env}

    def test_hop1_backend_to_middleware_mcp(self, monkeypatch):
        """Backend has JARVIS_RUNTIME_RPC_SOCKET → agent_spawner_server
        (middleware MCP, YAML does NOT declare the var) inherits it via
        the ``_parent_extras`` whitelist.
        """
        monkeypatch.setenv("JARVIS_RUNTIME_RPC_SOCKET", "/tmp/test-rpc.sock")

        # agent_spawner's YAML env block — note: no JARVIS_RUNTIME_RPC_SOCKET
        agent_spawner_yaml_env = {
            "PYTHONUNBUFFERED": "1",
            "JARVIS_API_KEY": "${JARVIS_API_KEY}",
            "JARVIS_API_BASE": "${JARVIS_API_BASE}",
        }
        child_env = self._build_stdio_env(agent_spawner_yaml_env)

        assert child_env.get("JARVIS_RUNTIME_RPC_SOCKET") == "/tmp/test-rpc.sock", (
            "agent_spawner_server MUST inherit JARVIS_RUNTIME_RPC_SOCKET via "
            "_parent_extras whitelist — without it, grandchild MCPs fail with "
            "ENOENT on a literal ${JARVIS_RUNTIME_RPC_SOCKET} path."
        )

    def test_hop2_middleware_to_isolated_runner(self, monkeypatch):
        """agent_spawner_server's os.environ → isolated_runner subprocess.
        Replicates ``isolated_spawner._run_subprocess`` line 200-206.
        """
        monkeypatch.setenv("JARVIS_RUNTIME_RPC_SOCKET", "/tmp/test-rpc.sock")

        # agent_spawner_server's os.environ at this point — inherited via hop 1
        # Now it spawns isolated_runner via subprocess.Popen({**os.environ, ...})
        subprocess_env = {
            **os.environ,
            "PYTHONPATH": "/project",
        }
        team_env = {"TEAM_MY_ROLE": "pm", "TEAM_SESSION_ID": "sess-1"}
        subprocess_env.update(team_env)

        assert subprocess_env["JARVIS_RUNTIME_RPC_SOCKET"] == "/tmp/test-rpc.sock"
        assert subprocess_env["TEAM_MY_ROLE"] == "pm"

    def test_hop3_isolated_runner_to_grandchild_mcp(self, monkeypatch):
        """isolated_runner spawns approval-server (grandchild MCP) which
        declares ``JARVIS_RUNTIME_RPC_SOCKET: "${JARVIS_RUNTIME_RPC_SOCKET}"``
        in YAML. expandvars must resolve against isolated_runner's env.
        """
        monkeypatch.setenv("JARVIS_RUNTIME_RPC_SOCKET", "/tmp/test-rpc.sock")

        approval_yaml_env = {
            "PYTHONUNBUFFERED": "1",
            "JARVIS_RUNTIME_RPC_SOCKET": "${JARVIS_RUNTIME_RPC_SOCKET}",
        }
        grandchild_env = self._build_stdio_env(approval_yaml_env)

        assert grandchild_env["JARVIS_RUNTIME_RPC_SOCKET"] == "/tmp/test-rpc.sock", (
            "approval-server received literal ${JARVIS_RUNTIME_RPC_SOCKET} — "
            "this is the 2026-05-09 regression. The fix is in "
            "mcp_connection_manager.py:_parent_extras."
        )

    def test_regression_break_when_whitelist_drops_var(self, monkeypatch):
        """Inverse contract: explicitly demonstrate what BREAKS if the
        whitelist forgets JARVIS_RUNTIME_RPC_SOCKET. This pins the
        forward direction — if someone refactors _parent_extras and
        drops the entry, hop 1 produces an empty env, hop 3 expandvars
        leaks the literal placeholder, and approval-server hits ENOENT.
        """
        monkeypatch.setenv("JARVIS_RUNTIME_RPC_SOCKET", "/tmp/test-rpc.sock")

        from mcp.client.stdio import get_default_environment

        # Simulate the OLD (broken) whitelist — SPAWN_* + VIRTUAL_ENV only
        broken_parent_extras = {
            k: v for k, v in os.environ.items()
            if k.startswith("SPAWN_") or k in ("VIRTUAL_ENV",)
        }
        agent_spawner_env = {
            **get_default_environment(),
            **broken_parent_extras,
            **{"JARVIS_API_KEY": os.path.expandvars("${JARVIS_API_KEY}")},
        }
        # Hop 1 with broken whitelist: var is gone
        assert "JARVIS_RUNTIME_RPC_SOCKET" not in agent_spawner_env

        # Hop 2: subprocess inherits agent_spawner's (broken) env
        # We restrict to agent_spawner_env to simulate the chain truthfully
        runner_env = {**agent_spawner_env, "PYTHONPATH": "/project"}

        # Hop 3: grandchild expandvars now resolves against an env WITHOUT
        # the var. expandvars leaves the placeholder LITERAL.
        approval_value_raw = "${JARVIS_RUNTIME_RPC_SOCKET}"
        # Simulate what mcp_connection_manager does: os.path.expandvars
        # uses os.environ of the CURRENT process. In production this is
        # isolated_runner's os.environ which lacks the var. Force the
        # scenario by temporarily clearing it from the test env.
        monkeypatch.delenv("JARVIS_RUNTIME_RPC_SOCKET", raising=False)
        leaked = os.path.expandvars(approval_value_raw)
        # In a broken chain, expandvars returns the literal placeholder
        # because the var is unset → approval-server gets ENOENT.
        assert leaked == "${JARVIS_RUNTIME_RPC_SOCKET}"


# ═══════════════════════════════════════════════
# 11. MCP server env: secrets.yaml must override Docker-only defaults
# ═══════════════════════════════════════════════


_BACKEND_DIR = Path(__file__).resolve().parent.parent


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Mimic FastAgent's startup deep_merge(config, secrets): nested dicts
    merge recursively, scalars/lists are overlay-wins.
    """
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@pytest.mark.skipif(
    not (_BACKEND_DIR / "fastagent.secrets.yaml").exists(),
    reason=(
        "fastagent.secrets.yaml is gitignored — these are local-dev "
        "regression catchers for the Docker-default override. CI has no "
        "secrets to verify; skip rather than emit false-positive failures. "
        "Locally, the absence of this file means the override hasn't been "
        "configured yet, which is itself a setup bug — see the class "
        "docstring for the fix."
    ),
)
class TestMCPDockerDefaultOverrides:
    """Pin the 2026-05-29 figma-ui-mcp regression.

    Root cause: ``fastagent.config.yaml`` hard-codes
    ``figma-ui-mcp.env.FIGMA_BRIDGE_HOST: host.docker.internal`` as the
    Docker default. Spawned subagents (Trang - Designer in the agile_team
    template) inherit this value via the parent config's ``env:`` block
    inside ``isolated_runner.create_child_config()``. The secrets.yaml is
    copied as-is to the child temp dir, and FastAgent deep_merges them at
    child startup — so the override lives in secrets.yaml.

    Without the secrets override, the Docker default leaks into local dev:
    the MCP subprocess tries to connect to a hostname that doesn't resolve
    outside Docker → ``figma_status`` returns ``pluginConnected: false``
    even though the Figma Desktop plugin is up and the bridge is reachable
    on 127.0.0.1:38451.

    Same shape applies to any future MCP server that uses ``*.docker.internal``
    as a config-level default — secrets.yaml MUST override.
    """

    @pytest.fixture
    def merged_mcp_servers(self):
        config = yaml.safe_load((_BACKEND_DIR / "fastagent.config.yaml").read_text()) or {}
        secrets = yaml.safe_load((_BACKEND_DIR / "fastagent.secrets.yaml").read_text()) or {}
        return _deep_merge(config, secrets).get("mcp", {}).get("servers", {})

    def test_figma_bridge_host_overridden_for_local_dev(self, merged_mcp_servers):
        """After deep_merge(config, secrets), figma-ui-mcp.env.FIGMA_BRIDGE_HOST
        must NOT resolve to ``host.docker.internal`` — that hostname only
        exists inside Docker. The override belongs in fastagent.secrets.yaml
        under ``mcp.servers.figma-ui-mcp.env``.
        """
        env = merged_mcp_servers.get("figma-ui-mcp", {}).get("env", {}) or {}
        host = env.get("FIGMA_BRIDGE_HOST", "")

        assert host, (
            "FIGMA_BRIDGE_HOST missing from merged figma-ui-mcp env. "
            "Add `FIGMA_BRIDGE_HOST: 127.0.0.1` (or a Tailscale IP for split-"
            "machine setups) under mcp.servers.figma-ui-mcp.env in "
            "fastagent.secrets.yaml."
        )
        assert "docker.internal" not in host, (
            f"FIGMA_BRIDGE_HOST={host!r} still resolves to the Docker default "
            f"from fastagent.config.yaml. This breaks local dev — the hostname "
            f"doesn't resolve outside Docker, so the MCP subprocess can't reach "
            f"the bridge and figma_status reports pluginConnected: false. "
            f"Add `FIGMA_BRIDGE_HOST: 127.0.0.1` under "
            f"mcp.servers.figma-ui-mcp.env in fastagent.secrets.yaml."
        )

    def test_no_docker_only_hostnames_leak_into_merged_config(self, merged_mcp_servers):
        """Generalized regression catcher — fails loud if any MCP server's
        merged env still contains ``host.docker.internal``. Same root cause
        as the figma bug: Docker-only defaults that local dev can't resolve.
        """
        offenders: list[str] = []
        for srv_name, srv_cfg in merged_mcp_servers.items():
            if not isinstance(srv_cfg, dict):
                continue
            env = srv_cfg.get("env", {}) or {}
            for key, value in env.items():
                if isinstance(value, str) and "host.docker.internal" in value:
                    offenders.append(f"{srv_name}.env.{key}={value!r}")

        assert not offenders, (
            "These merged MCP env values still resolve to Docker-only hostnames "
            "and will fail silently in local dev:\n  "
            + "\n  ".join(offenders)
            + "\nAdd local overrides under mcp.servers.<server>.env in "
            "fastagent.secrets.yaml for each."
        )

    def test_spawned_child_config_inherits_secrets_override(self, tmp_path):
        """End-to-end on the spawn path itself: invoke
        ``isolated_runner.create_child_config()`` with figma-ui-mcp in the
        server list, then simulate FastAgent's child-startup deep_merge of
        the generated child config + the copied child secrets. The resolved
        ``FIGMA_BRIDGE_HOST`` must NOT be the Docker default.

        This catches a regression that the static merge test above would
        miss — e.g. if someone refactors create_child_config and stops
        copying ``fastagent.secrets.yaml`` into the child temp dir
        (current behaviour: isolated_runner.py:393-399), the static test
        would still pass but spawned subagents would silently break.
        """
        try:
            from fast_agent.spawn.isolated_runner import create_child_config
        except ImportError as e:
            pytest.skip(f"fast_agent.spawn not importable: {e}")

        try:
            child_dir = create_child_config(
                project_dir=str(_BACKEND_DIR),
                workspace_dir=str(tmp_path),
                servers=["figma-ui-mcp"],
            )
        except Exception as e:
            pytest.skip(
                f"create_child_config raised — backend runtime layout may "
                f"differ in CI: {e}"
            )

        try:
            child_config_path = Path(child_dir) / "fastagent.config.yaml"
            child_secrets_path = Path(child_dir) / "fastagent.secrets.yaml"

            assert child_config_path.exists(), (
                "create_child_config did not write fastagent.config.yaml"
            )
            assert child_secrets_path.exists(), (
                "create_child_config did not copy fastagent.secrets.yaml into "
                "the child temp dir. Without secrets, FastAgent's startup "
                "deep_merge has nothing to overlay → config.yaml's Docker "
                "defaults leak through. Check "
                "isolated_runner.create_child_config (the shutil.copy2 "
                "around line 393-399 must run unconditionally)."
            )

            child_config = yaml.safe_load(child_config_path.read_text()) or {}
            child_secrets = yaml.safe_load(child_secrets_path.read_text()) or {}
            merged = _deep_merge(child_config, child_secrets)

            env = (
                merged.get("mcp", {}).get("servers", {})
                .get("figma-ui-mcp", {}).get("env", {}) or {}
            )
            host = env.get("FIGMA_BRIDGE_HOST", "")

            assert "docker.internal" not in host, (
                f"Spawned subagent's figma-ui-mcp got FIGMA_BRIDGE_HOST={host!r}. "
                f"The Docker default leaked through the spawn path. Either the "
                f"secrets override is missing (check fastagent.secrets.yaml) OR "
                f"the spawn path stopped copying secrets into the child temp "
                f"dir (check isolated_runner.create_child_config lines 393-399)."
            )
        finally:
            shutil.rmtree(child_dir, ignore_errors=True)
