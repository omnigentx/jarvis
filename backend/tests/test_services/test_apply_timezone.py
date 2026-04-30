"""Tests for runtime_config.apply_timezone and its _on_config_change dispatch.

Kept separate from test_runtime_config.py because that file imports
services.shared_state (which needs fast_agent) — these tests only need
runtime_config, so they run in any environment.
"""
from __future__ import annotations

import os

import pytest

from services import runtime_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("JARVIS_TIMEZONE", raising=False)


def _event(category, key, *, new_value, action):
    from services.config_service import ConfigChangeEvent
    return ConfigChangeEvent(
        category=category, key=key,
        old_value=None, new_value=new_value,
        is_secret=False, action=action,
    )


class TestApplyTimezone:
    def test_valid_timezone_sets_env(self):
        runtime_config.apply_timezone("Europe/Paris")
        assert os.environ["JARVIS_TIMEZONE"] == "Europe/Paris"

    def test_returns_normalised_name(self):
        result = runtime_config.apply_timezone("  UTC  ")
        assert result == "UTC"

    def test_whitespace_stripped(self):
        runtime_config.apply_timezone("  Asia/Tokyo  ")
        assert os.environ["JARVIS_TIMEZONE"] == "Asia/Tokyo"

    def test_invalid_timezone_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown timezone"):
            runtime_config.apply_timezone("Not/Real/Zone")

    def test_invalid_timezone_does_not_pollute_env(self):
        try:
            runtime_config.apply_timezone("Fake/Zone")
        except ValueError:
            pass
        assert "JARVIS_TIMEZONE" not in os.environ

    def test_none_falls_back_to_default(self):
        result = runtime_config.apply_timezone(None)
        assert result == runtime_config._DEFAULT_TIMEZONE
        assert os.environ["JARVIS_TIMEZONE"] == runtime_config._DEFAULT_TIMEZONE

    def test_empty_string_falls_back_to_default(self):
        result = runtime_config.apply_timezone("")
        assert result == runtime_config._DEFAULT_TIMEZONE

    def test_various_valid_iana_zones_accepted(self):
        zones = [
            "UTC",
            "Asia/Ho_Chi_Minh",
            "Asia/Tokyo",
            "Europe/London",
            "America/New_York",
            "America/Los_Angeles",
            "Pacific/Auckland",
        ]
        for tz in zones:
            runtime_config.apply_timezone(tz)
            assert os.environ["JARVIS_TIMEZONE"] == tz, f"Failed for {tz}"


class TestTimezoneListenerDispatch:
    def test_timezone_update_event_dispatches(self, monkeypatch):
        calls = []
        monkeypatch.setattr(runtime_config, "apply_timezone", lambda tz: calls.append(tz))
        runtime_config._on_config_change(
            _event("system", "TIMEZONE", new_value="Asia/Tokyo", action="update")
        )
        assert calls == ["Asia/Tokyo"]

    def test_timezone_delete_event_passes_none(self, monkeypatch):
        calls = []
        monkeypatch.setattr(runtime_config, "apply_timezone", lambda tz: calls.append(tz))
        runtime_config._on_config_change(
            _event("system", "TIMEZONE", new_value=None, action="delete")
        )
        assert calls == [None]

    def test_listener_catches_apply_timezone_exception(self, monkeypatch):
        """A broken apply_timezone must not crash the config change listener."""
        monkeypatch.setattr(runtime_config, "apply_timezone", lambda _: (_ for _ in ()).throw(ValueError("bad tz")))
        runtime_config._on_config_change(
            _event("system", "TIMEZONE", new_value="Bad/Zone", action="update")
        )

    def test_unrelated_system_key_does_not_call_apply_timezone(self, monkeypatch):
        calls = []
        monkeypatch.setattr(runtime_config, "apply_timezone", lambda tz: calls.append(tz))
        runtime_config._on_config_change(
            _event("system", "LOG_CONSOLE_LEVEL", new_value="DEBUG", action="update")
        )
        assert calls == []

    def test_timezone_key_in_non_system_category_ignored(self, monkeypatch):
        """Only system/TIMEZONE triggers apply_timezone, not e.g. user/TIMEZONE."""
        calls = []
        monkeypatch.setattr(runtime_config, "apply_timezone", lambda tz: calls.append(tz))
        runtime_config._on_config_change(
            _event("user", "TIMEZONE", new_value="UTC", action="update")
        )
        assert calls == []
