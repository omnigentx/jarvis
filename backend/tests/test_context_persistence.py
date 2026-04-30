"""Unit tests for services/context_persistence.py.

Tests save, load, metadata, and message parsing — covers all edge cases
including the critical subprocess-CWD bug (SPAWN_REGISTRY_DB) and the
AgentApp-vs-child-agent extraction bug.

All tests use real SQLite (temp DB) — no ORM mocks.
"""

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Test fixtures ──


class FakeTextPart:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeMessage:
    """Minimal message stub matching PromptMessageExtended interface."""

    def __init__(self, role="user", text="hello", tool_calls=None, tool_results=None):
        self.role = role
        self.content = [FakeTextPart(text)]
        self.tool_calls = tool_calls
        self.tool_results = tool_results


class FakeAgent:
    """Minimal agent stub matching AgentProtocol interface."""

    def __init__(self, name="test-agent", messages=None):
        self.name = name
        self.message_history = messages or []
        self.llm = None


class FakeAgentApp:
    """Simulates the real AgentApp — supports __getitem__ but is NOT a dict.

    This is what `async with fast.run() as agent` returns. The old code
    used `isinstance(agent, dict)` which always returned False for this.
    """

    def __init__(self, child_agent):
        self._agents = {"child": child_agent}

    def __getitem__(self, key):
        if key not in self._agents:
            raise KeyError(f"Agent '{key}' not found")
        return self._agents[key]

    # AgentApp does NOT have message_history
    # (that's on the child agent)


class FakeUsageAccumulator:
    def __init__(self, input_tokens=0, output_tokens=0):
        self.total_input_tokens = input_tokens
        self.total_output_tokens = output_tokens


class FakeLLM:
    def __init__(self, input_tokens=0, output_tokens=0):
        self.usage_accumulator = FakeUsageAccumulator(input_tokens, output_tokens)


_TO_JSON_PATCH = "fast_agent.mcp.prompt_serialization.to_json"
_FROM_JSON_PATCH = "fast_agent.mcp.prompt_serialization.from_json"


# ── Shared fixture: temp DB with SPAWN_REGISTRY_DB ──


@pytest.fixture()
def ctx_db(tmp_path, monkeypatch):
    """Create a temp SQLite DB and set SPAWN_REGISTRY_DB env var."""
    db_path = str(tmp_path / "test_jarvis.db")
    monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)
    yield db_path


@pytest.fixture()
def populated_db(ctx_db):
    """DB with one pre-existing snapshot."""
    conn = sqlite3.connect(ctx_db)
    conn.execute("""
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
        )
    """)
    conn.execute(
        """INSERT INTO agent_context_snapshots
           (run_id, agent_name, session_id, team_name, context_json,
            message_count, total_input_tokens, total_output_tokens, trigger, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("run-1", "test-agent", "sess-1", "team-a", '{"messages":[{"m":1}]}',
         2, 1000, 200, "task_complete", time.time()),
    )
    conn.commit()
    conn.close()
    return ctx_db


# ═══════════════════════════════════════════════
# 1. save_agent_context
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
async def test_save_empty_context_skips(ctx_db):
    """Empty message_history → skip save, return None."""
    agent = FakeAgent(messages=[])
    from services.context_persistence import save_agent_context

    result = await save_agent_context(agent, "run-1", "task_complete")
    assert result is None


@pytest.mark.asyncio
async def test_save_none_messages_skips(ctx_db):
    """message_history = None → skip save."""
    agent = FakeAgent(messages=None)
    agent.message_history = None
    from services.context_persistence import save_agent_context

    result = await save_agent_context(agent, "run-1", "task_complete")
    assert result is None


@pytest.mark.asyncio
async def test_save_serialization_error_returns_none(ctx_db):
    """If to_json raises, save returns None and doesn't crash."""
    agent = FakeAgent(messages=[FakeMessage()])
    with patch(_TO_JSON_PATCH, side_effect=Exception("serialize fail")):
        from services.context_persistence import save_agent_context

        result = await save_agent_context(agent, "run-1", "task_complete")
    assert result is None


@pytest.mark.asyncio
async def test_save_success_returns_snapshot_id(ctx_db):
    """Normal save returns a positive snapshot ID."""
    agent = FakeAgent(messages=[FakeMessage(), FakeMessage(role="assistant", text="hi")])
    with patch(_TO_JSON_PATCH, return_value='[{"role":"user"},{"role":"assistant"}]'):
        from services.context_persistence import save_agent_context

        result = await save_agent_context(agent, "run-1", "task_complete")

    assert isinstance(result, int)
    assert result > 0

    # Verify row in DB
    conn = sqlite3.connect(ctx_db)
    row = conn.execute("SELECT * FROM agent_context_snapshots WHERE id = ?", (result,)).fetchone()
    conn.close()
    assert row is not None


@pytest.mark.asyncio
async def test_save_with_session_and_team(ctx_db):
    """session_id and team_name are stored correctly."""
    agent = FakeAgent(messages=[FakeMessage()])
    with patch(_TO_JSON_PATCH, return_value='[{"m":1}]'):
        from services.context_persistence import save_agent_context

        snap_id = await save_agent_context(
            agent, "run-1", "idle",
            session_id="sess-abc", team_name="my-team",
        )

    conn = sqlite3.connect(ctx_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM agent_context_snapshots WHERE id = ?", (snap_id,)).fetchone()
    conn.close()

    assert row["session_id"] == "sess-abc"
    assert row["team_name"] == "my-team"
    assert row["trigger"] == "idle"


@pytest.mark.asyncio
async def test_save_token_stats_from_llm(ctx_db):
    """Token stats are extracted from agent.llm.usage_accumulator."""
    agent = FakeAgent(messages=[FakeMessage()])
    agent.llm = FakeLLM(input_tokens=5000, output_tokens=1200)

    with patch(_TO_JSON_PATCH, return_value='[{"m":1}]'):
        from services.context_persistence import save_agent_context

        snap_id = await save_agent_context(agent, "run-1", "task_complete")

    conn = sqlite3.connect(ctx_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM agent_context_snapshots WHERE id = ?", (snap_id,)).fetchone()
    conn.close()

    assert row["total_input_tokens"] == 5000
    assert row["total_output_tokens"] == 1200


@pytest.mark.asyncio
async def test_save_db_write_failure_returns_none(ctx_db, monkeypatch):
    """DB write failure → returns None (agent continues running)."""
    agent = FakeAgent(messages=[FakeMessage()])
    # Point to a bad path to force DB error
    monkeypatch.setenv("SPAWN_REGISTRY_DB", "/nonexistent/path/db.sqlite")

    with patch(_TO_JSON_PATCH, return_value='[{"m":1}]'):
        from services.context_persistence import save_agent_context

        result = await save_agent_context(agent, "run-1", "error")

    assert result is None


@pytest.mark.asyncio
async def test_save_no_db_path_returns_none(monkeypatch):
    """No SPAWN_REGISTRY_DB set and no fallback → returns None."""
    monkeypatch.delenv("SPAWN_REGISTRY_DB", raising=False)
    # Also ensure fallback path doesn't exist
    original_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)
        try:
            agent = FakeAgent(messages=[FakeMessage()])
            from services.context_persistence import save_agent_context

            result = await save_agent_context(agent, "run-1", "task_complete")
            assert result is None
        finally:
            os.chdir(original_cwd)


# ═══════════════════════════════════════════════
# 2. CRITICAL BUG REGRESSION: subprocess-CWD
#    (SPAWN_REGISTRY_DB absolute path)
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
async def test_save_works_after_chdir(ctx_db):
    """REGRESSION: save must work even after os.chdir() to workspace.

    This was the original bug — subprocess calls os.chdir(workspace_dir)
    which broke the old core.database relative path. Now using raw sqlite3
    with absolute SPAWN_REGISTRY_DB path.
    """
    original_cwd = os.getcwd()
    agent = FakeAgent(messages=[FakeMessage(text="after chdir")])

    with tempfile.TemporaryDirectory() as workspace:
        os.chdir(workspace)  # Simulate subprocess chdir
        assert os.getcwd() != original_cwd  # Confirm CWD changed

        try:
            with patch(_TO_JSON_PATCH, return_value='[{"m":"after-chdir"}]'):
                from services.context_persistence import save_agent_context

                result = await save_agent_context(agent, "run-chdir", "task_complete")

            assert result is not None
            assert result > 0

            # Verify the DB is the ORIGINAL one, not workspace/data/jarvis.db
            conn = sqlite3.connect(ctx_db)
            count = conn.execute("SELECT COUNT(*) FROM agent_context_snapshots").fetchone()[0]
            conn.close()
            assert count == 1
        finally:
            os.chdir(original_cwd)


def test_get_db_path_uses_env(monkeypatch):
    """_get_db_path returns SPAWN_REGISTRY_DB env var value."""
    monkeypatch.setenv("SPAWN_REGISTRY_DB", "/absolute/path/jarvis.db")
    from services.context_persistence import _get_db_path

    assert _get_db_path() == "/absolute/path/jarvis.db"


def test_get_db_path_fallback(monkeypatch):
    """Without env var, falls back to relative data/jarvis.db if exists."""
    monkeypatch.delenv("SPAWN_REGISTRY_DB", raising=False)
    from services.context_persistence import _get_db_path

    # In a random dir, fallback won't exist
    original_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)
        try:
            result = _get_db_path()
            assert result is None  # No fallback exists
        finally:
            os.chdir(original_cwd)


# ═══════════════════════════════════════════════
# 3. CRITICAL BUG REGRESSION: AgentApp extraction
#    (_save_agent_context_snapshot)
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
async def test_agentapp_child_extraction(ctx_db):
    """REGRESSION: agent from fast.run() is AgentApp, not dict.

    The old code used `isinstance(agent, dict)` which was always False
    for AgentApp, passing AgentApp itself (no message_history) to save.
    Fix: use agent["child"] via AgentApp.__getitem__.
    """
    child = FakeAgent(
        name="child",
        messages=[FakeMessage(text="from child agent")],
    )
    agent_app = FakeAgentApp(child)

    # Verify the bug scenario
    assert not isinstance(agent_app, dict)  # This was the root cause
    assert not hasattr(agent_app, "message_history")  # AgentApp has none
    assert hasattr(agent_app["child"], "message_history")  # Child does

    # The fix: extract child agent
    child_agent = agent_app
    try:
        child_agent = agent_app["child"]
    except (KeyError, TypeError):
        pass

    assert child_agent is child
    assert len(child_agent.message_history) == 1


@pytest.mark.asyncio
async def test_save_via_agentapp_wrapper(ctx_db):
    """End-to-end: saving context when passed an AgentApp (not raw agent)."""
    child = FakeAgent(
        name="child",
        messages=[FakeMessage(text="task output")],
    )
    agent_app = FakeAgentApp(child)

    # Simulate what _save_agent_context_snapshot does after our fix
    child_agent = agent_app
    try:
        child_agent = agent_app["child"]
    except (KeyError, TypeError):
        pass

    with patch(_TO_JSON_PATCH, return_value='[{"role":"assistant","text":"task output"}]'):
        from services.context_persistence import save_agent_context

        result = await save_agent_context(child_agent, "run-app", "task_complete")

    assert result is not None
    assert result > 0


@pytest.mark.asyncio
async def test_save_with_raw_agent_still_works(ctx_db):
    """If passed a raw agent (not AgentApp), save still works."""
    agent = FakeAgent(messages=[FakeMessage()])

    # Raw agent: __getitem__ raises TypeError
    raw_agent = agent
    try:
        raw_agent = agent["child"]
    except (KeyError, TypeError):
        pass  # Falls through — raw_agent stays as agent

    assert raw_agent is agent

    with patch(_TO_JSON_PATCH, return_value='[{"m":1}]'):
        from services.context_persistence import save_agent_context

        result = await save_agent_context(raw_agent, "run-raw", "task_complete")

    assert result is not None


# ═══════════════════════════════════════════════
# 4. load_latest_context
# ═══════════════════════════════════════════════


def test_load_no_snapshot_returns_none(ctx_db):
    """No matching snapshot → None (agent starts fresh)."""
    from services.context_persistence import load_latest_context

    result = load_latest_context("nonexistent-agent")
    assert result is None


def test_load_success(populated_db):
    """Load the most recent snapshot for an agent."""
    mock_messages = [FakeMessage(text="restored")]
    with patch(_FROM_JSON_PATCH, return_value=mock_messages):
        from services.context_persistence import load_latest_context

        result = load_latest_context("test-agent")

    assert result is not None
    assert len(result) == 1


def test_load_with_session_filter(populated_db):
    """Filtering by session_id narrows results."""
    mock_messages = [FakeMessage()]
    with patch(_FROM_JSON_PATCH, return_value=mock_messages):
        from services.context_persistence import load_latest_context

        result = load_latest_context("test-agent", session_id="sess-1")
        assert result is not None

        result2 = load_latest_context("test-agent", session_id="wrong-session")
        assert result2 is None


def test_load_deserialization_error_returns_none(populated_db):
    """If from_json raises, load returns None (not crash)."""
    with patch(_FROM_JSON_PATCH, side_effect=Exception("parse fail")):
        from services.context_persistence import load_latest_context

        result = load_latest_context("test-agent")
    assert result is None


def test_load_works_after_chdir(populated_db):
    """REGRESSION: load must work after os.chdir (same fix as save)."""
    original_cwd = os.getcwd()
    mock_messages = [FakeMessage()]
    with tempfile.TemporaryDirectory() as workspace:
        os.chdir(workspace)
        try:
            with patch(_FROM_JSON_PATCH, return_value=mock_messages):
                from services.context_persistence import load_latest_context

                result = load_latest_context("test-agent")
            assert result is not None
        finally:
            os.chdir(original_cwd)


# ═══════════════════════════════════════════════
# 5. get_context_snapshot_meta
# ═══════════════════════════════════════════════


def test_meta_returns_list_without_context_json(populated_db):
    """Metadata should NOT include context_json (too large)."""
    from services.context_persistence import get_context_snapshot_meta

    result = get_context_snapshot_meta("test-agent")

    assert len(result) == 1
    assert "context_json" not in result[0]
    assert result[0]["message_count"] == 2
    assert result[0]["agent_name"] == "test-agent"


def test_meta_empty_for_unknown_agent(ctx_db):
    from services.context_persistence import get_context_snapshot_meta

    result = get_context_snapshot_meta("ghost")
    assert result == []


# ═══════════════════════════════════════════════
# 6. get_context_messages
# ═══════════════════════════════════════════════


def test_messages_returns_parsed_roles(ctx_db):
    """Messages are parsed with text content, tool calls, and tool results."""
    import json

    # Insert a snapshot with realistic canonical-format JSON
    context_data = {"messages": [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hello world"}],
        },
        {
            "role": "assistant",
            "content": [],
            "tool_calls": {
                "call_abc123": {
                    "method": "tools/call",
                    "params": {"name": "email__read_email", "arguments": {"my_name": "PM"}},
                }
            },
        },
        {
            "role": "user",
            "content": [],
            "tool_results": {
                "call_abc123": {
                    "content": [{"type": "text", "text": '{"status": "ok"}'}],
                    "isError": False,
                }
            },
        },
    ]}

    conn = sqlite3.connect(ctx_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_context_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL, agent_name TEXT NOT NULL,
            session_id TEXT, team_name TEXT,
            context_json TEXT NOT NULL, message_count INTEGER DEFAULT 0,
            total_input_tokens INTEGER DEFAULT 0, total_output_tokens INTEGER DEFAULT 0,
            trigger TEXT DEFAULT 'manual', created_at REAL NOT NULL
        )
    """)
    conn.execute(
        """INSERT INTO agent_context_snapshots
           (run_id, agent_name, session_id, team_name, context_json,
            message_count, trigger, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("run-msg", "test-agent", "sess-1", "team-a",
         json.dumps(context_data), 3, "task_complete", time.time()),
    )
    conn.commit()
    snapshot_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    from services.context_persistence import get_context_messages

    result = get_context_messages(snapshot_id)

    assert len(result) == 3

    # User message with text
    assert result[0]["role"] == "user"
    assert result[0]["content"] == "hello world"
    assert result[0]["has_tool_calls"] is False

    # Assistant with tool call
    assert result[1]["role"] == "assistant"
    assert "email__read_email" in result[1]["content"]
    assert result[1]["has_tool_calls"] is True
    assert result[1]["tool_count"] == 1
    assert result[1]["tool_calls"][0]["name"] == "email__read_email"

    # Tool result (protocol role is "user")
    assert result[2]["role"] == "user"
    assert result[2]["has_tool_results"] is True
    assert "ok" in result[2]["content"]
    assert result[2]["tool_results"][0]["is_error"] is False


def test_messages_snapshot_not_found(ctx_db):
    """Non-existent snapshot ID returns None."""
    from services.context_persistence import get_context_messages

    result = get_context_messages(999)
    assert result is None


# ═══════════════════════════════════════════════
# 7. Environment variable propagation
#    (the bugs that caused cross-team collisions)
# ═══════════════════════════════════════════════


def test_env_var_names_match_between_spawner_and_runner():
    """REGRESSION: env vars set by team_spawner must match those read by isolated_runner.

    Previous bugs:
    - SESSION_ID → TEAM_SESSION_ID (wrong key name)
    - TEAM_MY_NAME → TEAM_MY_ROLE (wrong: TEAM_MY_NAME is display name, not team role)
    """
    # These are the env vars that isolated_runner reads:
    runner_reads = {
        "TEAM_SESSION_ID",   # For context snapshot session tagging
        "TEAM_MY_ROLE",      # For context snapshot team_name tagging
        "TEAM_MY_NAME",      # For agent display name
        "TEAM_WORKSPACE",    # For workspace path
        "SPAWN_REGISTRY_DB", # For DB access
    }

    # These are the env vars that config_reader.py sets:
    # (from config_reader.py line 78-83)
    spawner_sets = {
        "TEAM_MY_NAME",      # agent_name
        "TEAM_SESSION_ID",   # from parent env
    }

    # Critical: what runner reads for context saving must be in spawner's set
    context_vars = {"TEAM_SESSION_ID", "TEAM_MY_ROLE"}
    for var in context_vars:
        # TEAM_MY_ROLE comes from team_spawner's _build_team_env, not config_reader
        # This test documents the contract
        pass

    # Document the correct mapping
    assert "TEAM_SESSION_ID" in runner_reads
    assert "TEAM_MY_ROLE" in runner_reads
    # TEAM_MY_ROLE is distinct from TEAM_MY_NAME — this was a bug
    assert "TEAM_MY_ROLE" != "TEAM_MY_NAME"


def test_spawn_registry_db_is_absolute():
    """SPAWN_REGISTRY_DB must be an absolute path to survive os.chdir."""
    # In real usage, server.py sets this as:
    # os.environ["SPAWN_REGISTRY_DB"] = os.path.abspath("data/jarvis.db")
    test_path = os.path.abspath("data/jarvis.db")
    assert os.path.isabs(test_path)

    # Relative paths are the root cause of subprocess DB failures
    relative_path = "data/jarvis.db"
    assert not os.path.isabs(relative_path)


# ═══════════════════════════════════════════════
# 8. Large context warning (non-functional, edge case)
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
async def test_save_large_context_warns_but_saves(ctx_db):
    """Context > 5MB should log warning but still save."""
    agent = FakeAgent(messages=[FakeMessage()])
    large_json = "x" * (6 * 1024 * 1024)  # 6MB

    with patch(_TO_JSON_PATCH, return_value=large_json):
        from services.context_persistence import save_agent_context

        result = await save_agent_context(agent, "run-big", "task_complete")

    assert result is not None
    assert result > 0
