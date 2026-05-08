"""Pre-flight check: server boot must fail fast (clear error, exit 1)
when JARVIS_MASTER_KEY is missing — REGARDLESS of whether the DB
currently has encrypted rows.

The earlier "skip preflight when DB is empty" branch was removed: it
converted a config bug into a delayed mystery 500 (backend boots fine,
then UI save returns 500 because Fernet can't encrypt the brand-new
secret). Fail-loud at boot so operators see the env var name in the log,
not in a stack trace from a settings save.
"""
from __future__ import annotations

import pytest

from core.preflight import check_master_key_or_exit


class TestPreflight:
    def test_no_key_aborts(self, monkeypatch, caplog):
        """No JARVIS_MASTER_KEY in env → SystemExit(1) with a clear log
        line, regardless of DB state."""
        monkeypatch.delenv("JARVIS_MASTER_KEY", raising=False)
        with caplog.at_level("ERROR"):
            with pytest.raises(SystemExit) as exc_info:
                check_master_key_or_exit()
        assert exc_info.value.code == 1
        msg = " ".join(r.getMessage() for r in caplog.records)
        # Operator must see env var name + upgrade hint in the log.
        assert "JARVIS_MASTER_KEY" in msg
        assert "JARVIS_API_KEY" in msg  # upgrade-from-old-build hint

    def test_key_set_allows_boot(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MASTER_KEY", "preflight-test-key-xxx")
        check_master_key_or_exit()  # must return cleanly

    def test_empty_string_treated_as_missing(self, monkeypatch):
        """An explicitly empty JARVIS_MASTER_KEY is just as broken as
        unset — Fernet can't derive from empty input. Currently the
        check uses ``os.environ.get`` truthy test which already covers
        this; the test pins the behavior so a refactor doesn't regress
        it (e.g., by switching to ``"JARVIS_MASTER_KEY" in os.environ``)."""
        monkeypatch.setenv("JARVIS_MASTER_KEY", "")
        with pytest.raises(SystemExit):
            check_master_key_or_exit()
