"""Cross-layer tests for services.llm_provider_sync bootstrap soft-fail.

Pre-PR-#8 incident: a stale ``service.github.personal_access_token``
crashed backend boot from ``server.py:lifespan`` →
``git_credential_sync.reconcile_from_db``. The same shape of bug exists
in :mod:`services.llm_provider_sync`:

  * ``reconcile_from_db`` reads ``llm.{provider}_api_key`` (secret) for
    every supported provider at lifespan startup.
  * ``migrate_legacy_keys`` reads the legacy ``llm.api_key`` (secret)
    once on boot to migrate pre-D2 single-provider deployments.

A stale ciphertext under either path would propagate ``RuntimeError``
through the lifespan startup and crash-loop the container. These tests
exercise the REAL :class:`ConfigService` (no mock) so a contract drift
between :mod:`services.config_service` and :mod:`services.secret_utils`
is caught locally before reaching CD.
"""
from __future__ import annotations

import pytest

from core import auth as core_auth
from core import secrets_crypto


@pytest.fixture
def real_service(tmp_path, monkeypatch):
    """Real ConfigService backed by a throwaway DB + master key. Each
    test gets a fresh DB so rotated-key state from one test cannot leak
    into another."""
    from core.database import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from services.config_service import ConfigService

    key = "llm-sync-tests-master-key-xxxxx"
    monkeypatch.setenv("JARVIS_API_KEY", key)
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", key)
    secrets_crypto.reload_master_key()

    engine = create_engine(f"sqlite:///{tmp_path}/llm_sync.db", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    return ConfigService(db_factory=Session)


@pytest.fixture
def isolate_apply(monkeypatch):
    """``apply_llm_provider_change`` writes to env + a YAML file on disk,
    neither of which we want these tests to mutate. Stub it out and
    capture the calls so we can still assert *which* providers got
    applied vs. skipped."""
    from services import llm_provider_sync

    calls: list[tuple[str, str, str, str]] = []

    def _capture(provider, kind, value, action="update"):
        calls.append((provider, kind, value, action))

    monkeypatch.setattr(llm_provider_sync, "apply_llm_provider_change", _capture)
    return calls


def _rotate_master_key(monkeypatch, new_key: str = "rotated-master-key-yyy"):
    monkeypatch.setenv("JARVIS_API_KEY", new_key)
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", new_key)
    secrets_crypto.reload_master_key()


# ── reconcile_from_db ──────────────────────────────────────────────────


class TestReconcileFromDbDecryptFail:
    """Cross-layer: if one provider's api_key is stale, reconcile must
    still apply the other providers' clean keys instead of crashing."""

    def test_stale_provider_does_not_crash_or_block_others(
        self, real_service, monkeypatch, isolate_apply, caplog,
    ):
        from services import llm_provider_sync

        # Two providers configured under master key A:
        #  - openai: api_key (will go stale after rotation)
        #  - anthropic: base_url (plain — never goes through Fernet)
        real_service.set("llm", "openai_api_key", "sk-realA", is_secret=True)
        real_service.set("llm", "anthropic_base_url", "https://api.example.com",
                         is_secret=False)

        # Sanity: clean read works under master key A.
        assert real_service.get("llm", "openai_api_key") == "sk-realA"

        # Rotate master key — openai_api_key ciphertext is now
        # un-decryptable; anthropic_base_url is plain so still readable.
        _rotate_master_key(monkeypatch)

        with pytest.raises(RuntimeError, match="could not be decrypted"):
            real_service.get("llm", "openai_api_key")

        # reconcile_from_db must NOT raise on the stale openai_api_key.
        with caplog.at_level("WARNING"):
            llm_provider_sync.reconcile_from_db(real_service)

        # The clean field still landed in apply_llm_provider_change.
        assert ("anthropic", "base_url", "https://api.example.com",
                "update") in isolate_apply
        # The stale field did NOT (safe_get_or_none returned None →
        # the `if value:` guard skipped the apply call).
        assert not any(
            provider == "openai" and kind == "api_key"
            for (provider, kind, *_rest) in isolate_apply
        )

        # Operator-visible warning identifies which key needs re-setting.
        assert any(
            "openai_api_key" in r.getMessage()
            and "could not be decrypted" in r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING"
        )


# ── migrate_legacy_keys ────────────────────────────────────────────────


class TestMigrateLegacyKeysDecryptFail:
    """Cross-layer: legacy single-provider ``llm.api_key`` is a *secret*.
    A stale value pre-rotation must not crash boot — the migration
    silently skips and runs cleanly next boot after the user re-enters
    the value via Settings → LLM."""

    def test_stale_legacy_api_key_skips_migration_without_crash(
        self, real_service, monkeypatch, caplog,
    ):
        from services import llm_provider_sync

        # Pre-D2 deployment: one secret legacy key + plain provider name.
        real_service.set("llm", "api_key", "sk-legacy", is_secret=True)
        real_service.set("llm", "provider", "anthropic", is_secret=False)

        # Sanity: clean read works under master key A.
        assert real_service.get("llm", "api_key") == "sk-legacy"

        # Rotate master key.
        _rotate_master_key(monkeypatch)
        with pytest.raises(RuntimeError, match="could not be decrypted"):
            real_service.get("llm", "api_key")

        # migrate_legacy_keys must NOT raise. The function returns None;
        # what we care about is that the legacy row still exists (no
        # destructive cleanup happened against an undecryptable secret),
        # so when the user re-sets via Settings → LLM the next boot
        # migration runs cleanly.
        with caplog.at_level("WARNING"):
            llm_provider_sync.migrate_legacy_keys(real_service)

        # Confirm operator-visible warning surfaced (so they know which
        # row to fix).
        assert any(
            "api_key" in r.getMessage()
            and "could not be decrypted" in r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING"
        )

    def test_clean_legacy_api_key_still_migrates(
        self, real_service, monkeypatch,
    ):
        """Sanity check — without this, ``test_stale_…`` could pass
        because migrate_legacy_keys is a no-op for unrelated reasons.
        Pin the happy-path round-trip too."""
        from services import llm_provider_sync

        # Stub the YAML / env writes that apply_llm_provider_change
        # performs internally — same trick as TestReconcileFromDbDecryptFail
        # but inline because this test is the only one in its class.
        real_service.set("llm", "api_key", "sk-legacy", is_secret=True)
        real_service.set("llm", "provider", "anthropic", is_secret=False)

        # `set_many` is the migration's transactional write. Don't stub
        # it — assert the legacy keys really moved.
        llm_provider_sync.migrate_legacy_keys(real_service)

        # api_key under the active provider's namespace is now set.
        assert real_service.get("llm", "anthropic_api_key") == "sk-legacy"
        # Legacy key removed.
        assert real_service.get("llm", "api_key") is None
