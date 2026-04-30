"""Tests for session_service.py — chat session management."""

from unittest.mock import patch, MagicMock

import pytest

from services.session_service import SessionService


class TestSessionService:
    """Tests for SessionService operations."""

    def test_init_creates_instance(self):
        """SessionService should initialize without error."""
        svc = SessionService()
        assert svc is not None
        assert svc._manager is not None

    def test_create_session_returns_dict(self):
        """create_session should return dict with id and title."""
        svc = SessionService()
        result = svc.create_session("Test Chat")
        assert "id" in result
        assert result["title"] == "Test Chat"

    def test_create_session_default_title(self):
        """create_session with no title should use 'New Chat'."""
        svc = SessionService()
        result = svc.create_session()
        assert result["title"] == "New Chat"

    def test_list_sessions_returns_list(self):
        """list_sessions should return a list."""
        svc = SessionService()
        result = svc.list_sessions()
        assert isinstance(result, list)

    def test_multiple_sessions_unique_ids(self):
        """Each created session should have a unique ID."""
        svc = SessionService()
        s1 = svc.create_session("Chat 1")
        s2 = svc.create_session("Chat 2")
        assert s1["id"] != s2["id"]
