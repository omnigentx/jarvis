"""Tests for scripts/rotate_master_key.py.

Two invariants matter:

1. **Constants pinned to core.secrets_crypto.** The script mirrors
   ``_HKDF_SALT``, ``_HKDF_INFO`` and ``_TOKEN_PREFIX`` so it can derive
   Fernet keys without importing the singleton-bearing module. If the
   real module's constants ever drift, the script silently encrypts
   under different parameters than the running backend reads — a
   nightmare to debug. Pin them.

2. **End-to-end re-encrypt round-trip.** Set up a real DB with two
   encrypted rows under master key A, run the script's ``main()``, then
   verify each row decrypts cleanly under master key B (and is still
   undecryptable under A).
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def rotate_module(monkeypatch):
    """Import the script module fresh each test so its module-level
    sys.path tweak doesn't bleed between tests."""
    backend_dir = Path(__file__).resolve().parents[2]
    scripts_dir = backend_dir / "scripts"
    monkeypatch.syspath_prepend(str(scripts_dir))
    if "rotate_master_key" in sys.modules:
        del sys.modules["rotate_master_key"]
    return importlib.import_module("rotate_master_key")


class TestMirroredConstants:
    """If these drift from core.secrets_crypto, the script silently
    re-encrypts under wrong parameters. The test fails loudly instead."""

    def test_hkdf_salt_matches(self, rotate_module):
        from core import secrets_crypto
        assert rotate_module._HKDF_SALT == secrets_crypto._HKDF_SALT

    def test_hkdf_info_matches(self, rotate_module):
        from core import secrets_crypto
        assert rotate_module._HKDF_INFO == secrets_crypto._HKDF_INFO

    def test_token_prefix_matches(self, rotate_module):
        from core import secrets_crypto
        assert rotate_module._TOKEN_PREFIX == secrets_crypto._TOKEN_PREFIX

    def test_derive_produces_same_fernet_as_secrets_crypto(self, rotate_module, monkeypatch):
        """Cross-check: a token encrypted by rotate_module._derive(K)
        must decrypt under secrets_crypto with the same K. If derivation
        diverges (different salt/info/length), this fails."""
        from core import secrets_crypto

        master = "cross-check-master-xxx"
        monkeypatch.setenv("JARVIS_MASTER_KEY", master)
        secrets_crypto.reload_master_key()

        f = rotate_module._derive(master)
        token = "v1:" + f.encrypt(b"hello").decode("ascii")
        assert secrets_crypto.decrypt(token) == "hello"


class TestRotateRoundTrip:
    """End-to-end: write secrets under key A, run script, read under B."""

    @pytest.fixture()
    def isolated_db(self, tmp_path, monkeypatch):
        """Throwaway DB so we don't touch the test session's main DB.
        Patch SessionLocal/SystemConfig the script imports lazily."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from core.database import Base
        from core import database as core_db

        engine = create_engine(f"sqlite:///{tmp_path}/rotate.db", future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)
        monkeypatch.setattr(core_db, "SessionLocal", Session)
        return Session

    def test_main_re_encrypts_every_secret(
        self, rotate_module, isolated_db, monkeypatch, capsys,
    ):
        from core import secrets_crypto
        from core.secrets_crypto import DecryptError
        from services.config_service import ConfigService

        # Set up: two encrypted secrets + one plain row, all under key A.
        monkeypatch.setenv("JARVIS_MASTER_KEY", "rotate-tests-key-A-xxxxx")
        secrets_crypto.reload_master_key()

        svc = ConfigService(db_factory=isolated_db)
        svc.set("service.github", "personal_access_token", "ghp_real",
                is_secret=True)
        svc.set("llm", "openai_api_key", "sk-prod", is_secret=True)
        svc.set("system", "TIMEZONE", "Asia/Ho_Chi_Minh", is_secret=False)

        # Sanity: secrets readable under key A.
        assert svc.get("service.github", "personal_access_token") == "ghp_real"
        assert svc.get("llm", "openai_api_key") == "sk-prod"

        # Run the rotate script with OLD=A, NEW=B.
        monkeypatch.setenv("JARVIS_MASTER_KEY_OLD", "rotate-tests-key-A-xxxxx")
        monkeypatch.setenv("JARVIS_MASTER_KEY_NEW", "rotate-tests-key-B-yyyyy")
        rc = rotate_module.main()
        assert rc == 0

        out = capsys.readouterr().out
        assert "Rotated 2 secret" in out

        # Switch the running process to key B — both secrets must now
        # decrypt cleanly, and a fresh DecryptError under key A confirms
        # the rotation actually changed ciphertexts.
        monkeypatch.setenv("JARVIS_MASTER_KEY", "rotate-tests-key-B-yyyyy")
        secrets_crypto.reload_master_key()
        assert svc.get("service.github", "personal_access_token") == "ghp_real"
        assert svc.get("llm", "openai_api_key") == "sk-prod"
        # Plain row untouched.
        assert svc.get("system", "TIMEZONE") == "Asia/Ho_Chi_Minh"

        # And under the OLD key the ciphertext is now garbage.
        monkeypatch.setenv("JARVIS_MASTER_KEY", "rotate-tests-key-A-xxxxx")
        secrets_crypto.reload_master_key()
        with pytest.raises(DecryptError):
            svc.get("service.github", "personal_access_token")

    def test_main_rejects_missing_env(self, rotate_module, monkeypatch, capsys):
        monkeypatch.delenv("JARVIS_MASTER_KEY_OLD", raising=False)
        monkeypatch.delenv("JARVIS_MASTER_KEY_NEW", raising=False)
        rc = rotate_module.main()
        assert rc == 2
        assert "JARVIS_MASTER_KEY_OLD" in capsys.readouterr().err

    def test_main_rejects_identical_keys(self, rotate_module, monkeypatch, capsys):
        monkeypatch.setenv("JARVIS_MASTER_KEY_OLD", "same")
        monkeypatch.setenv("JARVIS_MASTER_KEY_NEW", "same")
        rc = rotate_module.main()
        assert rc == 2
        assert "identical" in capsys.readouterr().err

    def test_main_skips_undecryptable_rows(
        self, rotate_module, isolated_db, monkeypatch, capsys,
    ):
        """A row that can't be decrypted under OLD key (e.g. legacy
        garbage from an even earlier rotation) must be reported as
        skipped, not crash the whole rotate."""
        from core import secrets_crypto
        from core.database import SystemConfig
        from services.config_service import ConfigService

        monkeypatch.setenv("JARVIS_MASTER_KEY", "rotate-tests-key-A-xxxxx")
        secrets_crypto.reload_master_key()

        svc = ConfigService(db_factory=isolated_db)
        svc.set("service.github", "personal_access_token", "ghp_clean",
                is_secret=True)

        # Inject a row with a v1-shaped but garbage-payload token.
        with isolated_db() as db:
            db.add(SystemConfig(
                category="service.bad", key="STALE_TOKEN",
                value="v1:bogus-ciphertext-never-encrypted-under-any-key",
                is_secret=True,
            ))
            db.commit()

        monkeypatch.setenv("JARVIS_MASTER_KEY_OLD", "rotate-tests-key-A-xxxxx")
        monkeypatch.setenv("JARVIS_MASTER_KEY_NEW", "rotate-tests-key-B-yyyyy")
        rc = rotate_module.main()
        assert rc == 0

        out = capsys.readouterr().out
        assert "Rotated 1 secret" in out
        assert "Skipped 1 row" in out
        assert "service.bad/STALE_TOKEN" in out
