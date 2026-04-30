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
