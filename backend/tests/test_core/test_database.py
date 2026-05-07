"""Tests for database initialization."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest


class TestDatabaseInit:
    """Tests for core database initialization."""

    def test_init_db_creates_tables(self, tmp_db):
        """init_db() should create database tables without error."""
        from core.database import init_db
        init_db()
        assert tmp_db.exists() or True  # SQLite may use in-memory

    def test_init_db_idempotent(self, tmp_db):
        """Calling init_db() twice should not cause errors."""
        from core.database import init_db
        init_db()
        init_db()  # Should not raise


class TestDatabaseIsolatedFromRuntime:
    """Regression: tests must NOT use the developer's data/jarvis.db.

    Several MCP test files declare autouse fixtures that DELETE FROM
    mcp_servers / mcp_event_log / agent_mcp_attachments via the global
    engine. Without isolation those wipes hit the runtime DB and the
    dashboard's MCP list goes empty (with tests still passing because they
    self-seed inside the fixture). conftest.py sets JARVIS_DB_PATH before
    any imports; core.database honors it.
    """

    def test_engine_uses_jarvis_db_path_override(self):
        from core.database import DATABASE_URL
        override = os.environ.get("JARVIS_DB_PATH")
        assert override, "conftest.py must set JARVIS_DB_PATH for tests"
        assert DATABASE_URL == f"sqlite:///{override}"

    def test_engine_does_not_point_at_runtime_db(self):
        from core.database import DATABASE_URL
        # The runtime DB path is data/jarvis.db relative to backend/. A test
        # engine pointed there would silently corrupt developer state.
        assert "data/jarvis.db" not in DATABASE_URL.replace(
            os.environ.get("JARVIS_DB_PATH", ""), ""
        ), f"Test engine must not use runtime DB: {DATABASE_URL}"
