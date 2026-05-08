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

  * stops swallowing the right RuntimeError shape,
  * swallows too much (e.g. "database is locked"),
  * the upstream message shape drifts and the marker no longer matches.

Module deliberately has no fast_agent dependency, so this whole file
runs locally without the submodule installed — making the cross-layer
decrypt-fail scenario reproducible on every developer's machine.
"""
from __future__ import annotations

import pytest

from core import auth as core_auth
from core import secrets_crypto
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


def test_swallows_decrypt_fail_runtime_error_and_calls_on_warn():
    """The wrapper returns None and forwards the original exception to
    on_warn — does not let it propagate."""
    def boom(c, k, d):
        raise RuntimeError(
            "service.github/personal_access_token: stored secret could "
            "not be decrypted (master key rotated without re-encrypting?)"
        )
    captured: list[Exception] = []
    result = secret_utils.safe_get_or_none(
        _StubService(boom),
        "service.github", "personal_access_token",
        on_warn=captured.append,
    )
    assert result is None
    assert len(captured) == 1
    assert isinstance(captured[0], RuntimeError)
    assert "could not be decrypted" in str(captured[0])


def test_swallows_decrypt_fail_without_on_warn_callback():
    """on_warn is optional — wrapper still returns None silently when
    omitted, so callers that don't care about the warning don't have to
    supply a no-op callback."""
    def boom(c, k, d):
        raise RuntimeError("oauth.google/client_id: stored secret could "
                           "not be decrypted")
    result = secret_utils.safe_get_or_none(
        _StubService(boom), "oauth.google", "client_id",
    )
    assert result is None  # no exception raised, no callback supplied


def test_propagates_other_runtime_errors():
    """A RuntimeError that ISN'T a decrypt fail (DB connection drop,
    missing table, lock contention …) must propagate. Broadening the
    catch would hide infrastructure problems behind a misleading
    silent None."""
    def db_error(c, k, d):
        raise RuntimeError("database is locked")
    with pytest.raises(RuntimeError, match="database is locked"):
        secret_utils.safe_get_or_none(_StubService(db_error), "x", "y")


def test_propagates_non_runtime_errors():
    """Programming bugs (AttributeError, ValueError, KeyError …) must
    propagate — soft-fail is scoped strictly to the InvalidToken-shaped
    RuntimeError. Otherwise broadening the except would hide real defects
    at boot."""
    def bug(c, k, d):
        raise ValueError("bad argument")
    with pytest.raises(ValueError, match="bad argument"):
        secret_utils.safe_get_or_none(_StubService(bug), "x", "y")


def test_marker_match_is_substring_not_exact():
    """The marker check must be an ``in`` substring test, not an exact
    equality, so wrappers that prefix/suffix the message (e.g. log
    formatters) still get swallowed."""
    def wrapped(c, k, d):
        raise RuntimeError(
            "[BOOTSTRAP] service.x/Y: stored secret could not be decrypted "
            "(rotated key) — see Settings → Services to fix."
        )
    captured: list[Exception] = []
    result = secret_utils.safe_get_or_none(
        _StubService(wrapped), "service.x", "Y", on_warn=captured.append,
    )
    assert result is None
    assert len(captured) == 1


# ── Cross-layer integration test (real Fernet + real ConfigService) ─────


@pytest.fixture()
def real_service(tmp_path, monkeypatch):
    """Real ConfigService backed by a throwaway DB + master key, so Fernet
    encrypt/decrypt round-trips work for real. Tests using this fixture
    exercise the actual contract between ConfigService and secret_utils,
    not a mock — that's the only way to catch a drift in the upstream
    RuntimeError message shape (which is what _DECRYPT_FAIL_MARKER
    pattern-matches).
    """
    key = "secret-utils-tests-master-key-xxxxx"
    monkeypatch.setenv("JARVIS_API_KEY", key)
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", key)
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

    This is the exact regression scenario CD has hit twice (PR #7 + #8).
    If a future refactor:

      * changes the upstream RuntimeError message shape so
        ``_DECRYPT_FAIL_MARKER`` no longer matches,
      * narrows the except clause in safe_get_or_none,
      * removes the wrapper entirely from a caller,

    this test fails BEFORE production CD crash-loops. The unit tests
    above can't catch any of those because both sides of the contract
    agree on synthetic strings.
    """
    # Step 1: store a real Fernet-encrypted secret under master key A.
    real_service.set(
        "service.github", "personal_access_token", "ghp_realsecret",
        is_secret=True,
    )
    # Sanity: clean read works under key A.
    assert real_service.get("service.github", "personal_access_token") \
        == "ghp_realsecret"

    # Step 2: rotate master key. The DB row's ciphertext is now
    # un-decryptable under the new key.
    rotated_key = "rotated-master-key-yyyyy"
    monkeypatch.setenv("JARVIS_API_KEY", rotated_key)
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", rotated_key)
    secrets_crypto.reload_master_key()

    # Sanity: reading the secret directly really does raise the
    # expected shape. If this assertion ever stops holding, the rest of
    # the test loses its teeth — pinning the trigger here means a
    # contract drift in ConfigService.get is caught explicitly rather
    # than appearing as a confusing "wrapper test passed but prod
    # crashed" later.
    with pytest.raises(RuntimeError, match="could not be decrypted"):
        real_service.get("service.github", "personal_access_token")

    # Step 3: the wrapper must NOT raise — bootstrap must proceed.
    captured: list[Exception] = []
    result = secret_utils.safe_get_or_none(
        real_service, "service.github", "personal_access_token",
        on_warn=captured.append,
    )
    assert result is None
    assert len(captured) == 1
    assert "could not be decrypted" in str(captured[0])
