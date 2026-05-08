"""Pre-flight check: server boot must fail fast (clear error, exit 1)
when JARVIS_MASTER_KEY is missing AND the DB has encrypted secrets.

Without this, the missing key would surface deep inside an unrelated
bootstrap step (e.g. wizard's Services step calling
``ensure_provider_sections``) with a confusing stack trace.

Fresh installs (no encrypted rows yet) must still boot — the key only
becomes mandatory when there's something to decrypt.
"""
from __future__ import annotations

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as core_db
from core.database import Base, SystemConfig
from core.preflight import check_master_key_or_exit


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/preflight.db", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    monkeypatch.setattr(core_db, "SessionLocal", Session)
    return Session


class TestPreflight:
    def test_no_secrets_no_key_allows_boot(self, isolated_db, monkeypatch):
        """Fresh install: empty DB + no key → must NOT raise. The key
        only becomes mandatory when there's encrypted data to read."""
        monkeypatch.delenv("JARVIS_MASTER_KEY", raising=False)
        check_master_key_or_exit()  # must return cleanly

    def test_secrets_present_no_key_aborts(
        self, isolated_db, monkeypatch, caplog,
    ):
        """Existing install upgraded without setting JARVIS_MASTER_KEY
        in env → must SystemExit with a clear log line, not crash later
        on a confusing decrypt error."""
        monkeypatch.delenv("JARVIS_MASTER_KEY", raising=False)
        with isolated_db() as db:
            db.add(SystemConfig(
                category="service.github", key="personal_access_token",
                value="v1:some-ciphertext", is_secret=True,
            ))
            db.commit()

        with caplog.at_level("ERROR"):
            with pytest.raises(SystemExit) as exc_info:
                check_master_key_or_exit()
        assert exc_info.value.code == 1
        # Must mention the env var name and the upgrade hint so operators
        # can self-diagnose without grepping the codebase.
        msg = " ".join(r.getMessage() for r in caplog.records)
        assert "JARVIS_MASTER_KEY" in msg
        assert "JARVIS_API_KEY" in msg  # upgrade-from-old-build hint

    def test_secrets_present_key_set_allows_boot(
        self, isolated_db, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_MASTER_KEY", "preflight-test-key-xxx")
        with isolated_db() as db:
            db.add(SystemConfig(
                category="service.github", key="personal_access_token",
                value="v1:some-ciphertext", is_secret=True,
            ))
            db.commit()
        check_master_key_or_exit()  # must return cleanly

    def test_only_plain_rows_no_key_allows_boot(self, isolated_db, monkeypatch):
        """Plain rows (is_secret=False) never go through Fernet, so a
        DB containing only plain rows must boot without JARVIS_MASTER_KEY."""
        monkeypatch.delenv("JARVIS_MASTER_KEY", raising=False)
        with isolated_db() as db:
            db.add(SystemConfig(
                category="system", key="TIMEZONE",
                value="Asia/Ho_Chi_Minh", is_secret=False,
            ))
            db.commit()
        check_master_key_or_exit()  # must return cleanly
