"""Tests for services.config_service — settings read/write with encryption + audit."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import secrets_crypto
from core.database import Base, ConfigHistory, SystemConfig
from services.config_service import (
    ConfigChangeEvent,
    ConfigEntry,
    ConfigService,
    SECRET_PLACEHOLDER,
)


# ---- Fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _crypto_master_key(monkeypatch):
    """Every test starts with a known master key + clean crypto state."""
    monkeypatch.setenv("JARVIS_API_KEY", "unit-test-master-key-xxxxxxxx")
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None
    yield
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None


@pytest.fixture()
def db_factory(tmp_path):
    """Fresh SQLite engine per test — tables created via ``Base.metadata``."""
    db_file = tmp_path / "config_test.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield SessionFactory
    engine.dispose()


@pytest.fixture()
def svc(db_factory):
    return ConfigService(db_factory=db_factory)


# ---- Non-secret values -------------------------------------------------------


class TestPlainRoundTrip:
    def test_set_then_get_returns_value(self, svc):
        svc.set("llm", "model", "gpt-4o-mini")
        assert svc.get("llm", "model") == "gpt-4o-mini"

    def test_get_missing_returns_none(self, svc):
        assert svc.get("llm", "nope") is None

    def test_get_missing_returns_explicit_default(self, svc):
        assert svc.get("llm", "nope", default="fallback") == "fallback"

    def test_set_overwrites_existing(self, svc):
        svc.set("llm", "model", "gpt-3.5")
        svc.set("llm", "model", "gpt-4o")
        assert svc.get("llm", "model") == "gpt-4o"


class TestNoEnvFallback:
    """DB is the single source of truth — env vars are never consulted.

    The pre-refactor ``get()`` had an opaque ``DB → env → default`` chain
    that could silently substitute an unrelated env value of the same
    name for a missing DB row. These tests pin the no-fallback contract
    so a future "convenience" tweak can't quietly reintroduce it.
    """

    def test_db_miss_does_not_fall_back_to_env(self, svc, monkeypatch):
        monkeypatch.setenv("MODEL", "gpt-env")
        assert svc.get("llm", "MODEL") is None

    def test_db_miss_with_env_set_still_uses_explicit_default(
        self, svc, monkeypatch
    ):
        monkeypatch.setenv("MODEL", "gpt-env")
        assert svc.get("llm", "MODEL", default="gpt-default") == "gpt-default"

    def test_db_hit_with_conflicting_env_returns_db(self, svc, monkeypatch):
        svc.set("llm", "MODEL", "gpt-db")
        monkeypatch.setenv("MODEL", "gpt-env")
        assert svc.get("llm", "MODEL") == "gpt-db"

    def test_get_rejects_legacy_env_var_kwarg(self, svc):
        # The old ``env_var=`` kwarg was the explicit hook for env-fallback.
        # It's gone — calling code that still passes it must fail loudly so
        # the migration is mechanical, not silent.
        with pytest.raises(TypeError):
            svc.get("llm", "base_url", env_var="OPENAI_API_BASE")  # type: ignore[call-arg]

    def test_get_rejects_legacy_env_fallback_kwarg(self, svc):
        with pytest.raises(TypeError):
            svc.get("llm", "MODEL", env_fallback=False)  # type: ignore[call-arg]


# ---- Secret values -----------------------------------------------------------


class TestSecretRoundTrip:
    def test_secret_roundtrip_is_transparent(self, svc, db_factory):
        svc.set("auth", "api_key", "sk-prod-xyz", is_secret=True)
        assert svc.get("auth", "api_key") == "sk-prod-xyz"

        # Stored as ciphertext — never as plaintext.
        with db_factory() as db:
            row = (
                db.query(SystemConfig)
                .filter_by(category="auth", key="api_key")
                .one()
            )
        assert row.value is not None
        assert row.value != "sk-prod-xyz"
        assert row.value.startswith("v1:")
        assert row.is_secret is True

    def test_secret_masked_in_list(self, svc):
        svc.set("auth", "api_key", "sk-prod-xyz", is_secret=True)
        entries = svc.list_category("auth")
        assert len(entries) == 1
        assert entries[0].is_secret is True
        assert entries[0].value == SECRET_PLACEHOLDER
        assert entries[0].has_value is True

    def test_secret_masked_in_get_entry(self, svc):
        svc.set("auth", "api_key", "sk-prod-xyz", is_secret=True)
        entry = svc.get_entry("auth", "api_key")
        assert entry is not None
        assert entry.value == SECRET_PLACEHOLDER
        assert entry.has_value is True

    def test_undecryptable_secret_raises(self, svc, monkeypatch):
        """Decryption failure must surface loudly, not silently substitute env.

        The pre-refactor behaviour fell through to ``os.environ`` so a user
        wasn't "locked out" after a master-key rotation. That hid two real
        bugs: (1) post-rotation cleanup never ran, leaving the DB stuffed
        with dead ciphertext; (2) an unrelated env var with the same name
        could leak in as the answer to a totally different question. Now
        we refuse and tell the user to re-set the value.
        """
        svc.set("auth", "api_key", "sk-original", is_secret=True)
        # Rotate master key so the ciphertext can no longer be read.
        monkeypatch.setenv("JARVIS_API_KEY", "rotated-master-key-yyyyyy")
        secrets_crypto.reload_master_key()
        # Even with a tempting env var present, we must NOT consume it.
        monkeypatch.setenv("api_key", "sk-from-env")
        with pytest.raises(RuntimeError, match="could not be decrypted"):
            svc.get("auth", "api_key")


# ---- Delete ------------------------------------------------------------------


class TestDelete:
    def test_delete_via_none_value(self, svc, db_factory):
        svc.set("llm", "model", "gpt-4o")
        event = svc.set("llm", "model", None)
        assert event.action == "delete"
        with db_factory() as db:
            assert (
                db.query(SystemConfig)
                .filter_by(category="llm", key="model")
                .one_or_none()
                is None
            )

    def test_delete_missing_returns_noop(self, svc):
        assert svc.delete("llm", "never-existed") is False

    def test_delete_hit_returns_true(self, svc):
        svc.set("llm", "model", "gpt-4o")
        assert svc.delete("llm", "model") is True


# ---- Idempotence -------------------------------------------------------------


class TestIdempotence:
    def test_same_value_skips_history(self, svc):
        svc.set("llm", "model", "gpt-4o")
        history_before = svc.get_history(category="llm", key="model")
        svc.set("llm", "model", "gpt-4o")  # same value
        history_after = svc.get_history(category="llm", key="model")
        assert len(history_before) == len(history_after)

    def test_same_value_skips_listener(self, svc):
        events: list[ConfigChangeEvent] = []
        svc.subscribe(events.append)
        svc.set("llm", "model", "gpt-4o")
        assert len(events) == 1  # create
        svc.set("llm", "model", "gpt-4o")
        assert len(events) == 1  # unchanged


# ---- Listeners ---------------------------------------------------------------


class TestListeners:
    def test_subscribe_fires_on_create(self, svc):
        events: list[ConfigChangeEvent] = []
        svc.subscribe(events.append)
        svc.set("llm", "model", "gpt-4o")
        assert len(events) == 1
        assert events[0].action == "create"
        assert events[0].new_value == "gpt-4o"
        assert events[0].old_value is None

    def test_subscribe_fires_on_update(self, svc):
        events: list[ConfigChangeEvent] = []
        svc.set("llm", "model", "gpt-3.5")
        svc.subscribe(events.append)
        svc.set("llm", "model", "gpt-4o")
        assert len(events) == 1
        assert events[0].action == "update"
        assert events[0].old_value == "gpt-3.5"
        assert events[0].new_value == "gpt-4o"

    def test_subscribe_fires_on_delete(self, svc):
        events: list[ConfigChangeEvent] = []
        svc.set("llm", "model", "gpt-4o")
        svc.subscribe(events.append)
        svc.set("llm", "model", None)
        assert len(events) == 1
        assert events[0].action == "delete"

    def test_unsubscribe_stops_events(self, svc):
        events: list[ConfigChangeEvent] = []
        unsubscribe = svc.subscribe(events.append)
        svc.set("llm", "model", "gpt-4o")
        unsubscribe()
        svc.set("llm", "model", "gpt-4o-v2")
        assert len(events) == 1

    def test_unsubscribe_is_idempotent(self, svc):
        unsubscribe = svc.subscribe(lambda _e: None)
        unsubscribe()
        unsubscribe()  # must not raise

    def test_bad_listener_does_not_break_others(self, svc, caplog):
        good: list[ConfigChangeEvent] = []

        def boom(_event):
            raise RuntimeError("listener exploded")

        svc.subscribe(boom)
        svc.subscribe(good.append)
        with caplog.at_level("ERROR"):
            svc.set("llm", "model", "gpt-4o")
        assert len(good) == 1
        assert any("Listener" in r.message for r in caplog.records)

    def test_secret_event_carries_plaintext_to_listener(self, svc):
        """Listeners need the plain value to hot-reload runtime state."""
        events: list[ConfigChangeEvent] = []
        svc.subscribe(events.append)
        svc.set("auth", "api_key", "sk-live-123", is_secret=True)
        assert events[0].new_value == "sk-live-123"
        assert events[0].is_secret is True


# ---- list/list_all -----------------------------------------------------------


class TestListing:
    def test_list_category_is_sorted(self, svc):
        svc.set("llm", "temperature", "0.7")
        svc.set("llm", "model", "gpt-4o")
        svc.set("llm", "max_tokens", "4096")
        entries = svc.list_category("llm")
        keys = [e.key for e in entries]
        assert keys == sorted(keys)

    def test_list_category_empty_returns_empty_list(self, svc):
        assert svc.list_category("nonexistent") == []

    def test_list_all_groups_by_category(self, svc):
        svc.set("llm", "model", "gpt-4o")
        svc.set("auth", "api_key", "sk-x", is_secret=True)
        svc.set("llm", "temperature", "0.7")
        grouped = svc.list_all()
        assert set(grouped.keys()) == {"llm", "auth"}
        assert len(grouped["llm"]) == 2
        assert len(grouped["auth"]) == 1
        assert grouped["auth"][0].value == SECRET_PLACEHOLDER

    def test_get_entry_missing_returns_none(self, svc):
        assert svc.get_entry("llm", "missing") is None

    def test_entry_metadata_populated(self, svc):
        svc.set("llm", "model", "gpt-4o", source="wizard", user="setup")
        entry = svc.get_entry("llm", "model")
        assert isinstance(entry, ConfigEntry)
        assert entry.category == "llm"
        assert entry.key == "model"
        assert entry.source == "wizard"
        assert entry.updated_by == "setup"
        assert entry.is_secret is False
        assert entry.has_value is True


# ---- History -----------------------------------------------------------------


class TestHistory:
    def test_create_appends_history(self, svc):
        svc.set("llm", "model", "gpt-4o")
        history = svc.get_history(category="llm", key="model")
        assert len(history) == 1
        assert history[0].action == "create"
        assert history[0].old_value is None
        assert history[0].new_value == "gpt-4o"

    def test_update_appends_history(self, svc):
        svc.set("llm", "model", "gpt-3.5")
        svc.set("llm", "model", "gpt-4o")
        history = svc.get_history(category="llm", key="model")
        # Newest first.
        assert [h.action for h in history] == ["update", "create"]
        assert history[0].old_value == "gpt-3.5"
        assert history[0].new_value == "gpt-4o"

    def test_delete_appends_history(self, svc):
        svc.set("llm", "model", "gpt-4o")
        svc.set("llm", "model", None)
        history = svc.get_history(category="llm", key="model")
        assert history[0].action == "delete"
        assert history[0].new_value is None
        assert history[0].old_value == "gpt-4o"

    def test_secret_history_is_masked(self, svc, db_factory):
        svc.set("auth", "api_key", "sk-original", is_secret=True)
        svc.set("auth", "api_key", "sk-rotated", is_secret=True)

        with db_factory() as db:
            rows = db.query(ConfigHistory).order_by(ConfigHistory.id).all()
        assert len(rows) == 2
        # Create: old=None, new=*** (not ciphertext, not plaintext).
        assert rows[0].action == "create"
        assert rows[0].old_value is None
        assert rows[0].new_value == SECRET_PLACEHOLDER
        # Update: both sides masked.
        assert rows[1].action == "update"
        assert rows[1].old_value == SECRET_PLACEHOLDER
        assert rows[1].new_value == SECRET_PLACEHOLDER

    def test_history_filter_by_key(self, svc):
        svc.set("llm", "model", "gpt-4o")
        svc.set("llm", "temperature", "0.7")
        filtered = svc.get_history(category="llm", key="model")
        assert len(filtered) == 1
        assert filtered[0].key == "model"

    def test_history_limit(self, svc):
        for i in range(5):
            svc.set("llm", "model", f"v{i}")
        assert len(svc.get_history(limit=3)) == 3
        assert len(svc.get_history(limit=0)) == 0

    def test_history_no_filter_returns_all(self, svc):
        svc.set("llm", "model", "gpt-4o")
        svc.set("auth", "api_key", "k", is_secret=True)
        assert len(svc.get_history()) == 2


# ---- Bulk updates ------------------------------------------------------------


class TestSetMany:
    def test_set_many_commits_all(self, svc):
        events = svc.set_many(
            [
                ("llm", "model", "gpt-4o", False),
                ("llm", "temperature", "0.7", False),
                ("auth", "api_key", "sk-x", True),
            ]
        )
        assert len(events) == 3
        assert svc.get("llm", "model") == "gpt-4o"
        assert svc.get("auth", "api_key") == "sk-x"

    def test_set_many_rolls_back_on_error(self, svc, db_factory):
        svc.set("llm", "existing", "before")
        with pytest.raises(ValueError):
            svc.set_many(
                [
                    ("llm", "existing", "after", False),
                    ("", "bad-category", "x", False),  # triggers validation error
                ]
            )
        # First change must be rolled back.
        assert svc.get("llm", "existing") == "before"
        with db_factory() as db:
            assert db.query(SystemConfig).count() == 1

    def test_set_many_empty_is_noop(self, svc):
        assert svc.set_many([]) == []

    def test_set_many_emits_only_after_commit(self, svc):
        events: list[ConfigChangeEvent] = []
        svc.subscribe(events.append)
        try:
            svc.set_many(
                [
                    ("llm", "model", "gpt-4o", False),
                    ("", "invalid", "x", False),  # will raise
                ]
            )
        except ValueError:
            pass
        # No listener should have fired, even for the row that "succeeded".
        assert events == []

    def test_set_many_handles_deletes(self, svc):
        svc.set("llm", "model", "gpt-4o")
        svc.set("llm", "temperature", "0.7")
        events = svc.set_many(
            [
                ("llm", "model", None, False),
                ("llm", "temperature", "0.9", False),
            ]
        )
        assert [e.action for e in events] == ["delete", "update"]
        assert svc.get("llm", "model", default="GONE") == "GONE"
        assert svc.get("llm", "temperature") == "0.9"


# ---- Validation --------------------------------------------------------------


class TestValidation:
    def test_empty_category_rejected(self, svc):
        with pytest.raises(ValueError):
            svc.set("", "key", "value")

    def test_whitespace_category_rejected(self, svc):
        with pytest.raises(ValueError):
            svc.set("   ", "key", "value")

    def test_empty_key_rejected(self, svc):
        with pytest.raises(ValueError):
            svc.set("llm", "", "value")

    def test_oversized_category_rejected(self, svc):
        with pytest.raises(ValueError):
            svc.set("x" * 101, "key", "value")

    def test_oversized_key_rejected(self, svc):
        with pytest.raises(ValueError):
            svc.set("llm", "x" * 101, "value")

    def test_non_string_category_rejected(self, svc):
        with pytest.raises(ValueError):
            svc.set(None, "key", "value")  # type: ignore[arg-type]
