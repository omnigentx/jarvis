"""Tests for tools.time_server — timezone resolution and time tools.

Key invariants tested:
- _get_tz() reads JARVIS_TIMEZONE from env at call-time (not import-time)
- Invalid timezone is NOT swallowed — ZoneInfoNotFoundError propagates
- Default is Asia/Ho_Chi_Minh when env var is absent
- get_current_time() returns time in the configured zone
"""
from __future__ import annotations

import pytest
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Import lazily inside tests so monkeypatch can set env before _get_tz() runs.
# (The function reads os.environ at call-time, so import order is fine.)


class TestGetTz:
    def test_default_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("JARVIS_TIMEZONE", raising=False)
        from tools.time_server import _get_tz
        assert _get_tz() == ZoneInfo("Asia/Ho_Chi_Minh")

    def test_reads_valid_timezone_from_env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TIMEZONE", "Europe/Paris")
        from tools.time_server import _get_tz
        assert _get_tz() == ZoneInfo("Europe/Paris")

    def test_different_valid_timezones(self, monkeypatch):
        zones = [
            "America/New_York",
            "America/Los_Angeles",
            "Europe/London",
            "Asia/Tokyo",
            "UTC",
            "Asia/Ho_Chi_Minh",
        ]
        from tools.time_server import _get_tz
        for tz in zones:
            monkeypatch.setenv("JARVIS_TIMEZONE", tz)
            assert _get_tz() == ZoneInfo(tz), f"Failed for {tz}"

    def test_invalid_timezone_raises_not_swallowed(self, monkeypatch):
        """REGRESSION: previous code had try/except that silently fell back.
        Invalid timezone must propagate so the misconfiguration is visible."""
        monkeypatch.setenv("JARVIS_TIMEZONE", "Not/A/Real/Zone")
        from tools.time_server import _get_tz
        with pytest.raises(ZoneInfoNotFoundError):
            _get_tz()

    def test_empty_string_raises(self, monkeypatch):
        # ZoneInfo("") raises ValueError: "keys must be normalized relative paths"
        monkeypatch.setenv("JARVIS_TIMEZONE", "")
        from tools.time_server import _get_tz
        with pytest.raises((ZoneInfoNotFoundError, ValueError)):
            _get_tz()

    def test_typo_in_tz_name_raises(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TIMEZONE", "Asia/Hochiminh")  # missing underscore
        from tools.time_server import _get_tz
        with pytest.raises(ZoneInfoNotFoundError):
            _get_tz()

    def test_re_reads_env_on_each_call(self, monkeypatch):
        """_get_tz() must not cache the timezone — each call re-reads os.environ."""
        from tools.time_server import _get_tz

        monkeypatch.setenv("JARVIS_TIMEZONE", "UTC")
        assert _get_tz() == ZoneInfo("UTC")

        monkeypatch.setenv("JARVIS_TIMEZONE", "Asia/Ho_Chi_Minh")
        assert _get_tz() == ZoneInfo("Asia/Ho_Chi_Minh")


class TestGetCurrentTime:
    def test_returns_string_with_time_and_date(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TIMEZONE", "UTC")
        from tools.time_server import get_current_time
        result = get_current_time()
        assert isinstance(result, str)
        assert ":" in result  # HH:MM
        assert "/" in result  # DD/MM/YYYY

    def test_respects_configured_timezone(self, monkeypatch):
        """Times in UTC+7 (Asia/Ho_Chi_Minh) and UTC differ by 7 hours.
        This test verifies the zone is actually applied to the datetime."""
        from datetime import datetime, timezone as dt_tz
        from zoneinfo import ZoneInfo

        now_utc = datetime.now(dt_tz.utc)
        expected_hour_vn = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%H")

        monkeypatch.setenv("JARVIS_TIMEZONE", "Asia/Ho_Chi_Minh")
        from tools.time_server import get_current_time
        result = get_current_time()
        # Extract HH from "Bây giờ là HH:MM ngày DD/MM/YYYY."
        parts = result.split()
        time_part = parts[3]  # "HH:MM"
        hour_in_result = time_part.split(":")[0]
        assert hour_in_result == expected_hour_vn

    def test_invalid_timezone_propagates(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TIMEZONE", "Fake/Zone")
        from tools.time_server import get_current_time
        with pytest.raises(ZoneInfoNotFoundError):
            get_current_time()
