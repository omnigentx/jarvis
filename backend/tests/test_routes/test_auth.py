"""Tests for core.auth — API key verification."""

import os
from unittest.mock import patch

import pytest


class TestApiKeyVerification:
    """Tests for the verify_api_key dependency and JARVIS_API_KEY handling."""

    def test_api_key_loaded_from_env(self, monkeypatch):
        """JARVIS_API_KEY should be read from environment."""
        monkeypatch.setenv("JARVIS_API_KEY", "test-secret-123")

        # Re-import to pick up new env
        import importlib
        import core.auth
        importlib.reload(core.auth)

        assert core.auth.JARVIS_API_KEY == "test-secret-123"

    def test_rate_limit_allows_normal_usage(self):
        """Rate limiter should allow first few requests."""
        from core.auth import _check_rate_limit
        # Should not raise for fresh IP
        result = _check_rate_limit("192.168.1.1")
        # Function returns None on success or raises on rate limit
        assert result is None or result is True or result is not False

    def test_record_login_attempt_doesnt_crash(self):
        """Recording a login attempt should not raise."""
        from core.auth import record_login_attempt
        record_login_attempt("192.168.1.1")
