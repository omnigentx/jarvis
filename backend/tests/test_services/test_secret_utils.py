"""Tests for services.secret_utils.safe_get_or_none.

Pin the bootstrap-safe wrapper around ConfigService.get. Four call sites
depend on this contract:

  * services.runtime_config.reconcile_service_env
  * services.git_credential_sync.reconcile_from_db
  * services.llm_provider_sync.reconcile_from_db
  * services.llm_provider_sync.migrate_legacy_keys

Pre-PR-#8 incident: a stale secret encrypted under a rotated master key
crash-looped the backend container. These tests catch any regression
where the wrapper:

  * stops swallowing :class:`~core.secrets_crypto.DecryptError`,
  * swallows too much (e.g. ``RuntimeError("database is locked")``).

Module deliberately has no fast_agent dependency, so this whole file
runs locally without the submodule installed — making the cross-layer
decrypt-fail scenario reproducible on every developer's machine.
"""
from __future__ import annotations

import pytest

from core import secrets_crypto
from core.secrets_crypto import DecryptError
from services import secret_utils


# ── Unit tests (stub service) ──────────────────────────────────────────


class _StubService:
    """Minimal duck-typed stand-in for ConfigService — only ``get`` is
    used by safe_get_or_none. Each test wires the side effect it needs."""

    def __init__(self, side_effect):
        self._side_effect = side_effect

    def get(self, category, key, default=None):
        return self._side_effect(category, key, default)


def test_returns_value_on_normal_read():
    svc = _StubService(lambda c, k, d: "hello")
    assert secret_utils.safe_get_or_none(svc, "service.x", "Y") == "hello"


def test_returns_none_when_underlying_returns_none():
    svc = _StubService(lambda c, k, d: None)
    assert secret_utils.safe_get_or_none(svc, "service.x", "MISSING") is None


def test_swallows_decrypt_error_and_calls_on_warn():
    """The wrapper returns None and forwards the original exception to
    on_warn — does not let it propagate."""
    def boom(c, k, d):
        raise DecryptError(
            "service.github/personal_access_token: stored secret could "
            "not be decrypted"
        )
    captured: list[Exception] = []
    result = secret_utils.safe_get_or_none(
        _StubService(boom),
        "service.github", "personal_access_token",
        on_warn=captured.append,
    )
    assert result is None
    assert len(captured) == 1
    assert isinstance(captured[0], DecryptError)


def test_swallows_decrypt_error_without_on_warn_callback():
    """on_warn is optional — wrapper still returns None silently when
    omitted."""
    def boom(c, k, d):
        raise DecryptError("oauth.google/client_id: stored secret could "
                           "not be decrypted")
    result = secret_utils.safe_get_or_none(
        _StubService(boom), "oauth.google", "client_id",
    )
    assert result is None


def test_propagates_other_runtime_errors():
    """A RuntimeError that ISN'T a DecryptError (DB connection drop,
    missing table, lock contention …) must propagate. Broadening the
    catch would hide infrastructure problems behind a misleading
    silent None."""
    def db_error(c, k, d):
        raise RuntimeError("database is locked")
    with pytest.raises(RuntimeError, match="database is locked"):
        secret_utils.safe_get_or_none(_StubService(db_error), "x", "y")


def test_propagates_non_runtime_errors():
    """Programming bugs (AttributeError, ValueError, KeyError …) must
    propagate — soft-fail is scoped strictly to DecryptError."""
    def bug(c, k, d):
        raise ValueError("bad argument")
    with pytest.raises(ValueError, match="bad argument"):
        secret_utils.safe_get_or_none(_StubService(bug), "x", "y")


# ── Cross-layer integration test (real Fernet + real ConfigService) ─────


@pytest.fixture()
def real_service(tmp_path, monkeypatch):
    """Real ConfigService backed by a throwaway DB + master key, so Fernet
    encrypt/decrypt round-trips work for real. Tests using this fixture
    exercise the actual contract between ConfigService and secret_utils,
    not a mock — that's the only way to catch a contract drift in the
    upstream :class:`DecryptError` shape.
    """
    monkeypatch.setenv("JARVIS_MASTER_KEY", "secret-utils-tests-master-key-xxxxx")
    secrets_crypto.reload_master_key()

    from core.database import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from services.config_service import ConfigService

    engine = create_engine(f"sqlite:///{tmp_path}/secrets.db", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    return ConfigService(db_factory=Session)


def test_cross_layer_clean_read_returns_value(real_service):
    """Round-trip a real encrypted secret. Sanity check — without this,
    the rotation test below could be passing for the wrong reason (e.g.
    if the fixture didn't actually encrypt anything)."""
    real_service.set("service.x", "Y", "hello", is_secret=True)
    assert secret_utils.safe_get_or_none(real_service, "service.x", "Y") == "hello"


def test_cross_layer_real_decrypt_fail_with_rotated_key(
    real_service, monkeypatch,
):
    """Cross-layer integration with NO mock between safe_get_or_none and
    the Fernet ciphertext: store a real secret under master key A, rotate
    to key B, then assert safe_get_or_none soft-fails gracefully.

    This is the exact regression scenario CD has hit (PR #6/#7/#8). If a
    future refactor narrows the except clause in safe_get_or_none, or
    changes the exception type ConfigService raises, this test fails
    BEFORE production CD crash-loops.
    """
    # Step 1: store a real Fernet-encrypted secret under master key A.
    real_service.set(
        "service.github", "personal_access_token", "ghp_realsecret",
        is_secret=True,
    )
    assert real_service.get("service.github", "personal_access_token") \
        == "ghp_realsecret"

    # Step 2: rotate master key. The DB row's ciphertext is now
    # un-decryptable under the new key.
    monkeypatch.setenv("JARVIS_MASTER_KEY", "rotated-master-key-yyyyy")
    secrets_crypto.reload_master_key()

    # Sanity: reading the secret directly really does raise DecryptError.
    with pytest.raises(DecryptError):
        real_service.get("service.github", "personal_access_token")

    # Step 3: the wrapper must NOT raise — bootstrap must proceed.
    captured: list[Exception] = []
    result = secret_utils.safe_get_or_none(
        real_service, "service.github", "personal_access_token",
        on_warn=captured.append,
    )
    assert result is None
    assert len(captured) == 1
    assert isinstance(captured[0], DecryptError)
