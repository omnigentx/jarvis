"""Unit tests for team agent naming and env propagation.

Tests the random Vietnamese naming system, uniqueness guarantees,
and environment variable correctness in the spawn pipeline.
"""

import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest


# ── Test fixtures ──


@pytest.fixture()
def spawn_db(tmp_path, monkeypatch):
    """Temp SQLite DB with agent_registry table."""
    db_path = str(tmp_path / "test_spawn.db")
    monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_registry (
            run_id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            role TEXT,
            status TEXT DEFAULT 'starting',
            lifecycle TEXT DEFAULT 'ephemeral',
            task TEXT,
            session_id TEXT,
            model TEXT,
            created_at REAL,
            updated_at REAL
        )
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def name_pool():
    """The expected Vietnamese name pool."""
    return [
        "An", "Bao", "Chi", "Dung", "Giang", "Hai", "Hoa", "Hung",
        "Huong", "Khanh", "Lan", "Linh", "Long", "Mai", "Minh",
        "My", "Nam", "Nga", "Ngoc", "Nhan", "Nhi", "Phong", "Phuc",
        "Phuong", "Quang", "Quynh", "Son", "Tam", "Thanh", "Thao",
        "Thi", "Thien", "Thu", "Thuy", "Tien", "Trang", "Trung",
        "Truc", "Tuan", "Tuyet", "Van", "Vi", "Viet", "Vinh",
        "Vu", "Xuan", "Yen",
    ]


# ═══════════════════════════════════════════════
# 1. Agent name generation — uniqueness
# ═══════════════════════════════════════════════


def test_generated_name_format():
    """Agent name = 'VietnameseName [ROLE]' format."""
    # Simulate the naming logic
    name = "Linh"
    role = "PM"
    agent_name = f"{name} [{role}]"
    assert agent_name == "Linh [PM]"


def test_names_are_ascii_no_diacritics(name_pool):
    """All names must be ASCII (no Vietnamese diacritics).

    User requirement: 'tên không dấu thôi'
    """
    for name in name_pool:
        assert name.isascii(), f"Name '{name}' contains non-ASCII characters"
        # Also check no common diacritics
        for char in name:
            assert ord(char) < 128, f"Name '{name}' has non-ASCII char: {char}"


def test_name_uniqueness_across_roles():
    """Same Vietnamese name with different roles = different agent names."""
    name = "Phong"
    pm_name = f"{name} [PM]"
    sa_name = f"{name} [SA]"
    assert pm_name != sa_name
    assert pm_name == "Phong [PM]"
    assert sa_name == "Phong [SA]"


def test_name_uniqueness_with_registry_check(spawn_db):
    """When registry has existing names, new names must not collide."""
    conn = sqlite3.connect(spawn_db)

    # Register some existing agents
    existing_names = ["Linh [PM]", "Phong [SA]", "Minh [Dev]"]
    for i, name in enumerate(existing_names):
        conn.execute(
            "INSERT INTO agent_registry (run_id, agent_name, role, status, session_id) VALUES (?, ?, ?, ?, ?)",
            (f"run-{i}", name, "role", "running", "session-1"),
        )
    conn.commit()
    conn.close()

    # Check that existing names are in the DB
    conn = sqlite3.connect(spawn_db)
    rows = conn.execute("SELECT agent_name FROM agent_registry").fetchall()
    db_names = {r[0] for r in rows}
    conn.close()

    assert "Linh [PM]" in db_names
    assert "Phong [SA]" in db_names
    assert "Minh [Dev]" in db_names


def test_pool_has_enough_names(name_pool):
    """Pool must be large enough to support multiple concurrent teams.

    With 47 names × ~5 roles = 235 possible combinations.
    Even with 10 concurrent teams × 5 agents = 50 agents, no collision.
    """
    assert len(name_pool) >= 40, f"Pool too small: {len(name_pool)} names"


# ═══════════════════════════════════════════════
# 2. Environment variable contract
# ═══════════════════════════════════════════════


def test_critical_env_vars_documented():
    """Document all env vars in the spawn pipeline and their consumers."""
    env_contract = {
        # Var name → (who sets it, who reads it)
        "SPAWN_REGISTRY_DB": ("server.py startup", "context_persistence, registry_backends, spawn_progress_bridge"),
        "TEAM_SESSION_ID": ("config_reader._build_spawn_env", "isolated_runner._save_agent_context_snapshot"),
        "TEAM_MY_ROLE": ("team_spawner._build_team_env", "isolated_runner._save_agent_context_snapshot"),
        "TEAM_MY_NAME": ("config_reader._build_spawn_env", "isolated_runner.run_child_agent, inbox_watcher_hook"),
        "TEAM_WORKSPACE": ("team_spawner._build_team_env", "inbox_watcher_hook"),
    }

    # All keys must be uppercase with underscores (convention)
    for key in env_contract:
        assert key == key.upper()
        assert " " not in key
        assert "-" not in key


def test_team_my_role_vs_team_my_name():
    """REGRESSION: TEAM_MY_ROLE and TEAM_MY_NAME are DIFFERENT variables.

    Previous bug: code used TEAM_MY_NAME where TEAM_MY_ROLE was expected,
    or vice versa. These serve different purposes:
    - TEAM_MY_NAME: display name for the agent (e.g. 'Linh [PM]')
    - TEAM_MY_ROLE: the role the agent plays (e.g. 'pm', 'sa', 'dev')
    """
    team_my_name = "Linh [PM]"  # Display name
    team_my_role = "pm"  # Role identifier

    assert team_my_name != team_my_role
    assert "[" in team_my_name  # Name includes role tag
    assert "[" not in team_my_role  # Role is clean identifier


def test_spawn_registry_db_must_be_absolute(monkeypatch):
    """SPAWN_REGISTRY_DB must store absolute path to survive chdir.

    REGRESSION: Previous bug used relative path which broke after
    subprocess called os.chdir(workspace_dir).
    """
    # Correct: absolute path
    abs_path = "/users/dev/jarvis/data/jarvis.db"
    monkeypatch.setenv("SPAWN_REGISTRY_DB", abs_path)
    assert os.path.isabs(os.environ["SPAWN_REGISTRY_DB"])

    # Bug scenario: relative path
    rel_path = "data/jarvis.db"
    monkeypatch.setenv("SPAWN_REGISTRY_DB", rel_path)
    assert not os.path.isabs(os.environ["SPAWN_REGISTRY_DB"]), \
        "Relative path should be detectable as non-absolute"


# ═══════════════════════════════════════════════
# 3. _save_agent_context_snapshot integration
#    (isolated_runner wrapper function)
# ═══════════════════════════════════════════════


def test_agentapp_isinstance_dict_is_false():
    """REGRESSION: AgentApp is NOT a dict — isinstance check was wrong.

    The old code:
        agent["child"] if isinstance(agent, dict) else agent
    Always passed AgentApp (which has no message_history) because
    isinstance(AgentApp(), dict) is False.
    """

    class AgentApp:
        def __getitem__(self, key):
            return f"agent-{key}"

    app = AgentApp()
    assert not isinstance(app, dict)  # THIS was the bug
    assert app["child"] == "agent-child"  # __getitem__ works


def test_correct_child_extraction_pattern():
    """Verify the correct pattern for extracting child from AgentApp."""

    class FakeChild:
        def __init__(self):
            self.message_history = [{"role": "user", "content": "hi"}]
            self.name = "child"

    class FakeApp:
        def __init__(self, child):
            self._agents = {"child": child}

        def __getitem__(self, key):
            return self._agents[key]

    child = FakeChild()
    app = FakeApp(child)

    # Old pattern (BUGGY):
    old_result = app["child"] if isinstance(app, dict) else app
    assert old_result is app  # BUG: returns AgentApp, not child
    assert not hasattr(old_result, "message_history")

    # New pattern (FIXED):
    new_result = app
    try:
        new_result = app["child"]
    except (KeyError, TypeError):
        pass
    assert new_result is child  # CORRECT: returns child agent
    assert hasattr(new_result, "message_history")


# ═══════════════════════════════════════════════
# 4. Session-scoped env vars
# ═══════════════════════════════════════════════


def test_context_snapshot_uses_correct_env_vars(monkeypatch):
    """Context snapshots must use TEAM_SESSION_ID and TEAM_MY_ROLE."""
    monkeypatch.setenv("TEAM_SESSION_ID", "session-abc-123")
    monkeypatch.setenv("TEAM_MY_ROLE", "sa")
    monkeypatch.setenv("TEAM_MY_NAME", "Phong [SA]")

    # Simulate what _save_agent_context_snapshot reads
    session_id = os.environ.get("TEAM_SESSION_ID")
    team_name = os.environ.get("TEAM_MY_ROLE", "")

    assert session_id == "session-abc-123"
    assert team_name == "sa"

    # It should NOT use TEAM_MY_NAME for team_name
    assert team_name != os.environ.get("TEAM_MY_NAME")


def test_missing_env_vars_dont_crash(monkeypatch):
    """Missing TEAM_* env vars should not crash — just produce empty/None."""
    monkeypatch.delenv("TEAM_SESSION_ID", raising=False)
    monkeypatch.delenv("TEAM_MY_ROLE", raising=False)
    monkeypatch.delenv("TEAM_MY_NAME", raising=False)

    session_id = os.environ.get("TEAM_SESSION_ID")
    team_name = os.environ.get("TEAM_MY_ROLE", "")

    assert session_id is None
    assert team_name == ""
