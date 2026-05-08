"""Tests for services.runtime_config — hot-reload dispatch."""
from __future__ import annotations

import logging
import sys

import pytest

from core import auth as core_auth
from core import secrets_crypto
from services import runtime_config, shared_state


@pytest.fixture(autouse=True)
def _restore_env(monkeypatch):
    # Each test gets a clean env slate for the keys we mutate.
    for key in (
        "LOG_CONSOLE_LEVEL",
        "JARVIS_API_KEY",
        "JARVIS_TIMEZONE",
    ):
        monkeypatch.delenv(key, raising=False)


class TestApplyApiKey:
    def test_applies_to_env_and_auth(self):
        runtime_config.apply_api_key("x" * 32)
        assert core_auth.JARVIS_API_KEY == "x" * 32

    def test_does_not_reload_crypto(self, monkeypatch):
        """The whole point of the API/master split: rotating the auth
        password must NOT trigger a Fernet reload (which used to crash
        bootstrap when the master key drifted)."""
        called = []
        monkeypatch.setattr(
            secrets_crypto, "reload_master_key",
            lambda: called.append(True) or "fp",
        )
        runtime_config.apply_api_key("y" * 32)
        assert called == []

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            runtime_config.apply_api_key("   ")


class TestApplyMasterKey:
    def test_sets_env_and_reloads_crypto(self, monkeypatch):
        monkeypatch.setattr(secrets_crypto, "reload_master_key", lambda: "fp-abc")
        fingerprint = runtime_config.apply_master_key("z" * 32)
        assert fingerprint == "fp-abc"
        import os
        assert os.environ["JARVIS_MASTER_KEY"] == "z" * 32

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            runtime_config.apply_master_key("   ")


class TestApplyLogConsoleLevel:
    def test_updates_console_handler(self, monkeypatch):
        root = logging.getLogger()
        # Ensure there's exactly one StreamHandler to target.
        existing = [h for h in root.handlers if isinstance(h, logging.StreamHandler)
                    and not isinstance(h, logging.FileHandler)]
        if not existing:
            handler = logging.StreamHandler(sys.stdout)
            root.addHandler(handler)
            existing = [handler]

        runtime_config.apply_log_console_level("DEBUG")
        assert existing[0].level == logging.DEBUG

        runtime_config.apply_log_console_level("ERROR")
        assert existing[0].level == logging.ERROR

    def test_stores_in_env(self):
        runtime_config.apply_log_console_level("info")  # normalised to upper
        import os
        assert os.environ["LOG_CONSOLE_LEVEL"] == "INFO"

    def test_invalid_level_rejected(self):
        with pytest.raises(ValueError):
            runtime_config.apply_log_console_level("TRACE")

    def test_none_falls_back_to_warning(self):
        import os
        runtime_config.apply_log_console_level(None)
        assert os.environ["LOG_CONSOLE_LEVEL"] == "WARNING"


# TTS apply tests live in test_runtime_config_voice.py — the registry-driven
# JSON config replaced the legacy env-var TTSFactory entirely.


class TestListenerDispatch:
    def test_api_key_event_dispatches(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            runtime_config, "apply_api_key", lambda k: calls.append(("key", k))
        )
        event = _event("auth", "JARVIS_API_KEY", new_value="newkey", action="update")
        runtime_config._on_config_change(event)
        assert calls == [("key", "newkey")]

    def test_log_level_event_dispatches(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            runtime_config,
            "apply_log_console_level",
            lambda lvl: calls.append(lvl),
        )
        event = _event("system", "LOG_CONSOLE_LEVEL", new_value="DEBUG", action="update")
        runtime_config._on_config_change(event)
        assert calls == ["DEBUG"]

    def test_log_level_delete_uses_default(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            runtime_config,
            "apply_log_console_level",
            lambda lvl: calls.append(lvl),
        )
        event = _event("system", "LOG_CONSOLE_LEVEL", new_value=None, action="delete")
        runtime_config._on_config_change(event)
        assert calls == [None]

    def test_listener_survives_handler_exception(self, monkeypatch):
        def boom(lvl):
            raise RuntimeError("boom")

        monkeypatch.setattr(runtime_config, "apply_log_console_level", boom)
        event = _event("system", "LOG_CONSOLE_LEVEL", new_value="DEBUG", action="update")
        # Must not raise — a broken listener shouldn't poison the caller.
        runtime_config._on_config_change(event)

    def test_unrelated_keys_ignored(self, monkeypatch):
        sentinel = {"called": False}
        monkeypatch.setattr(
            runtime_config,
            "apply_api_key",
            lambda *_a, **_kw: sentinel.__setitem__("called", True),
        )
        event = _event("llm", "model", new_value="gpt-4o", action="update")
        runtime_config._on_config_change(event)
        assert sentinel["called"] is False


class TestRegisterConfigListeners:
    def test_subscribes_and_fires_end_to_end(self, monkeypatch, tmp_path):
        # Use a real ConfigService wired to a throwaway DB so the commit path
        # is exercised end-to-end.
        from core.database import Base
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from services.config_service import ConfigService

        engine = create_engine(f"sqlite:///{tmp_path}/rc.db", future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)
        service = ConfigService(db_factory=Session)

        calls = []
        monkeypatch.setattr(
            runtime_config,
            "apply_log_console_level",
            lambda lvl: calls.append(lvl),
        )
        unsubscribe = runtime_config.register_config_listeners(service)
        try:
            service.set("system", "LOG_CONSOLE_LEVEL", "INFO")
            assert calls == ["INFO"]
            service.set("system", "LOG_CONSOLE_LEVEL", "DEBUG")
            assert calls == ["INFO", "DEBUG"]
        finally:
            unsubscribe()


class TestReconcileServiceEnv:
    @pytest.fixture()
    def service(self, tmp_path, monkeypatch):
        # A throwaway ConfigService + master key so Fernet can encrypt/decrypt.
        monkeypatch.setenv("JARVIS_MASTER_KEY", "reconcile-tests-master-key-xxxxx")
        secrets_crypto.reload_master_key()

        from core.database import Base
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from services.config_service import ConfigService

        engine = create_engine(f"sqlite:///{tmp_path}/reconcile.db", future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)
        return ConfigService(db_factory=Session)

    def test_exports_service_rows_to_env(self, service, monkeypatch):
        import os
        monkeypatch.delenv("ROBOROCK_USERNAME", raising=False)
        monkeypatch.delenv("ROBOROCK_PASSWORD", raising=False)
        service.set("service.roborock", "ROBOROCK_USERNAME", "alice@example.com", is_secret=True)
        service.set("service.roborock", "ROBOROCK_PASSWORD", "s3cret", is_secret=True)

        count = runtime_config.reconcile_service_env(service)
        assert count == 2
        assert os.environ["ROBOROCK_USERNAME"] == "alice@example.com"
        assert os.environ["ROBOROCK_PASSWORD"] == "s3cret"

    def test_skips_non_env_shaped_keys(self, service, monkeypatch):
        import os
        monkeypatch.delenv("free_form_key", raising=False)
        service.set("service.roborock", "free_form_key", "value", is_secret=False)
        count = runtime_config.reconcile_service_env(service)
        assert count == 0
        assert "free_form_key" not in os.environ

    def test_skips_non_service_categories(self, service, monkeypatch):
        import os
        monkeypatch.delenv("JARVIS_API_KEY", raising=False)
        service.set("auth", "JARVIS_API_KEY", "should-not-leak", is_secret=True)
        count = runtime_config.reconcile_service_env(service)
        assert count == 0
        assert os.environ.get("JARVIS_API_KEY") != "should-not-leak"

    def test_respects_preexisting_env_override(self, service, monkeypatch):
        import os
        monkeypatch.setenv("ROBOROCK_USERNAME", "override-from-docker")
        service.set("service.roborock", "ROBOROCK_USERNAME", "from-db", is_secret=True)
        count = runtime_config.reconcile_service_env(service)
        assert count == 0
        assert os.environ["ROBOROCK_USERNAME"] == "override-from-docker"

    def test_skips_empty_values(self, service, monkeypatch):
        import os
        monkeypatch.delenv("ROBOROCK_USERNAME", raising=False)
        service.set("service.roborock", "ROBOROCK_USERNAME", "", is_secret=True)
        count = runtime_config.reconcile_service_env(service)
        assert count == 0
        assert "ROBOROCK_USERNAME" not in os.environ

    def test_undecryptable_secret_does_not_crash_bootstrap(
        self, service, monkeypatch, caplog,
    ):
        """Regression: a stale secret encrypted under a rotated master key
        used to abort the entire backend boot (RuntimeError from
        config_service.get bubbled up). Bootstrap must be resilient — skip
        the bad row, log a warning, continue with the rest. Otherwise CD
        deploys are blocked any time the master key changes.
        """
        import os
        monkeypatch.delenv("ROBOROCK_USERNAME", raising=False)
        monkeypatch.delenv("ROBOROCK_PASSWORD", raising=False)
        # Good secret — must still land in env after we skip the bad one.
        service.set("service.roborock", "ROBOROCK_USERNAME", "alice@example.com", is_secret=True)
        # Make ROBOROCK_PASSWORD raise on .get(), simulating an InvalidToken
        # from a master-key rotation that wasn't followed by a re-encrypt.
        original_get = service.get
        def _fail_on_password(category, key, default=None):
            if (category, key) == ("service.roborock", "ROBOROCK_PASSWORD"):
                raise secrets_crypto.DecryptError(
                    "service.roborock/ROBOROCK_PASSWORD: stored secret could "
                    "not be decrypted"
                )
            return original_get(category, key, default=default)
        service.set("service.roborock", "ROBOROCK_PASSWORD", "ignored", is_secret=True)
        monkeypatch.setattr(service, "get", _fail_on_password)

        # Must NOT raise.
        count = runtime_config.reconcile_service_env(service)
        assert count == 1
        assert os.environ.get("ROBOROCK_USERNAME") == "alice@example.com"
        assert "ROBOROCK_PASSWORD" not in os.environ
        # Caller-visible warning log so ops know which row needs re-setting.
        assert any(
            "ROBOROCK_PASSWORD" in r.message
            for r in caplog.records if r.levelname == "WARNING"
        )


def _event(category, key, *, new_value, action):
    from services.config_service import ConfigChangeEvent

    return ConfigChangeEvent(
        category=category,
        key=key,
        old_value=None,
        new_value=new_value,
        is_secret=False,
        action=action,
    )
