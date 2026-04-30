"""Tests for services.repo_config — DB-only repo URL resolution.

The post-refactor contract: ``get_repo_url()`` consults
``config_service`` for ``service.jarvis_repo / JARVIS_REPO_URL`` and
nothing else. No env fallback. No git-remote auto-detect. Missing → raise.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import secrets_crypto
from core.database import Base
from services import config_service as config_service_mod
from services import repo_config
from services.config_service import ConfigService


@pytest.fixture(autouse=True)
def _crypto_master_key(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", "unit-test-master-key-xxxxxxxx")
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None
    yield
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None


@pytest.fixture()
def isolated_config_service(tmp_path, monkeypatch):
    """Swap the module-level singleton with a fresh, isolated instance.

    ``repo_config`` imports the singleton at module import — patching the
    attribute on the singleton's module is the correct seam.
    """
    db_file = tmp_path / "repo_config_test.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    svc = ConfigService(db_factory=SessionFactory)
    # repo_config did ``from services.config_service import config_service``
    # so the binding lives on the repo_config module. Patch both seams so
    # any future indirect reference also picks up the test instance.
    monkeypatch.setattr(config_service_mod, "config_service", svc)
    monkeypatch.setattr(repo_config, "config_service", svc)
    yield svc
    engine.dispose()


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    """No test should leak via JARVIS_REPO_URL env."""
    monkeypatch.delenv("JARVIS_REPO_URL", raising=False)


class TestDBHit:
    def test_returns_value_when_db_has_it(self, isolated_config_service):
        isolated_config_service.set(
            "service.jarvis_repo",
            "JARVIS_REPO_URL",
            "https://github.com/owner/jarvis.git",
        )
        assert repo_config.get_repo_url() == "https://github.com/owner/jarvis.git"

    def test_strips_whitespace(self, isolated_config_service):
        isolated_config_service.set(
            "service.jarvis_repo",
            "JARVIS_REPO_URL",
            "  https://github.com/owner/jarvis.git\n",
        )
        assert repo_config.get_repo_url() == "https://github.com/owner/jarvis.git"

    def test_works_with_secret_storage(self, isolated_config_service):
        # The wizard hardcodes is_secret=True for every service field. Verify
        # the read path decrypts transparently.
        isolated_config_service.set(
            "service.jarvis_repo",
            "JARVIS_REPO_URL",
            "https://github.com/owner/jarvis.git",
            is_secret=True,
        )
        assert repo_config.get_repo_url() == "https://github.com/owner/jarvis.git"


class TestDBMissRaises:
    def test_raises_when_db_empty(self, isolated_config_service):
        with pytest.raises(RuntimeError, match="not configured"):
            repo_config.get_repo_url()

    def test_raises_when_db_value_empty_string(self, isolated_config_service):
        # ConfigService treats "" as a delete in set(); manually round-trip
        # through the underlying entry to simulate the empty-string corruption
        # case (e.g. a future migration that backfills blanks).
        isolated_config_service.set(
            "service.jarvis_repo",
            "JARVIS_REPO_URL",
            "   ",  # whitespace-only
        )
        with pytest.raises(RuntimeError, match="not configured"):
            repo_config.get_repo_url()


class TestEnvIgnored:
    """Env var must not be consulted — single source of truth is the DB."""

    def test_env_set_but_db_empty_still_raises(
        self, isolated_config_service, monkeypatch
    ):
        monkeypatch.setenv("JARVIS_REPO_URL", "https://github.com/sneaky/from-env.git")
        with pytest.raises(RuntimeError, match="not configured"):
            repo_config.get_repo_url()

    def test_env_does_not_override_db(self, isolated_config_service, monkeypatch):
        isolated_config_service.set(
            "service.jarvis_repo",
            "JARVIS_REPO_URL",
            "https://github.com/owner/jarvis.git",
        )
        monkeypatch.setenv("JARVIS_REPO_URL", "https://github.com/sneaky/from-env.git")
        assert repo_config.get_repo_url() == "https://github.com/owner/jarvis.git"


class TestGitRemoteIgnored:
    """The pre-refactor fallback to ``git remote get-url origin`` is gone."""

    def test_inside_real_git_checkout_still_raises(
        self, isolated_config_service, tmp_path, monkeypatch
    ):
        repo = tmp_path / "fake_checkout"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/x/sneaky.git"],
            cwd=repo,
            check=True,
        )
        monkeypatch.chdir(repo)
        with pytest.raises(RuntimeError, match="not configured"):
            repo_config.get_repo_url()


class TestSignature:
    def test_takes_no_args(self):
        # Future regressions where someone re-introduces a working_dir param
        # would silently be ignored under the DB-only contract. This test is
        # the canary.
        with pytest.raises(TypeError):
            repo_config.get_repo_url(Path("/tmp"))  # type: ignore[call-arg]
