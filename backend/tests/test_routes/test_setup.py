"""Tests for routes/setup.py — Setup Wizard HTTP endpoints."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import auth as core_auth
from core import secrets_crypto
from core.database import Base, SetupWizardStep, SystemConfig


# ---- Fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    """Every test starts with no master key + fresh crypto + a clean DB."""
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "")
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None
    yield
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None


@pytest.fixture()
def db_factory(tmp_path, monkeypatch):
    """Fresh SQLite DB.  Routes resolve sessions via core.database.SessionLocal,
    so we swap that in place."""
    db_file = tmp_path / "setup_test.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Seed wizard rows so the "status" endpoint doesn't just return an empty
    # skeleton.
    from core.database import SETUP_WIZARD_STEPS

    with SessionFactory() as db:
        for name in SETUP_WIZARD_STEPS:
            db.add(SetupWizardStep(step_name=name))
        db.commit()

    # Rebind module-level SessionLocal in both the database module and in any
    # module that imported it by name.
    import core.database as core_db
    from services import config_service as config_module

    monkeypatch.setattr(core_db, "SessionLocal", SessionFactory)
    monkeypatch.setattr(config_module, "SessionLocal", SessionFactory)
    # The module-level singleton captured the old factory in __init__; rebuild
    # it so writes land in the temp DB too.
    monkeypatch.setattr(
        config_module,
        "config_service",
        config_module.ConfigService(db_factory=SessionFactory),
    )
    # Re-import routes so they pick up the fresh singleton.
    import routes.setup as setup_routes
    import routes.settings as settings_routes

    monkeypatch.setattr(setup_routes, "config_service", config_module.config_service)
    monkeypatch.setattr(settings_routes, "config_service", config_module.config_service)

    yield SessionFactory
    engine.dispose()


@pytest.fixture()
def client(db_factory):
    from routes.setup import router as setup_router
    from routes.settings import router as settings_router

    app = FastAPI()
    app.include_router(setup_router)
    app.include_router(settings_router)
    return TestClient(app)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {core_auth.JARVIS_API_KEY}"}


# ---- /api/setup/status -------------------------------------------------------


class TestStatus:
    def test_status_empty_before_any_step(self, client):
        # No master key configured → status endpoint is open.
        resp = client.get("/api/setup/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["overall_complete"] is False
        assert body["current_step"] == "auth"
        assert [s["name"] for s in body["steps"]] == [
            "auth",
            "llm",
            "services",
            "yaml_config",
            "verify",
        ]


# ---- /api/setup/auth ---------------------------------------------------------


class TestAuthStep:
    def test_auth_step_generates_key_when_omitted(self, client, db_factory):
        resp = client.post("/api/setup/auth", json={})
        assert resp.status_code == 200
        assert core_auth.JARVIS_API_KEY  # applied
        body = resp.json()
        auth_step = next(s for s in body["steps"] if s["name"] == "auth")
        assert auth_step["completed"] is True
        assert auth_step["data"]["generated"] is True

        # Key persisted in DB.
        with db_factory() as db:
            row = (
                db.query(SystemConfig)
                .filter_by(category="auth", key="JARVIS_API_KEY")
                .one()
            )
        assert row.value == core_auth.JARVIS_API_KEY

    def test_auth_step_accepts_user_key(self, client):
        resp = client.post(
            "/api/setup/auth", json={"api_key": "user-chosen-master-key-abcd"}
        )
        assert resp.status_code == 200
        assert core_auth.JARVIS_API_KEY == "user-chosen-master-key-abcd"

    def test_auth_step_rejects_short_key(self, client):
        resp = client.post("/api/setup/auth", json={"api_key": "short"})
        assert resp.status_code == 400
        assert not core_auth.JARVIS_API_KEY

    def test_auth_step_refuses_different_key_when_already_set(self, client, monkeypatch):
        monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "already-configured-xxx")
        resp = client.post(
            "/api/setup/auth", json={"api_key": "new-key-1234567890123"}
        )
        assert resp.status_code == 403

    def test_auth_step_adopts_existing_key_when_blank(self, client, monkeypatch):
        """Fresh-container recovery: ``.env`` set JARVIS_API_KEY but wizard
        step isn't done yet → empty submit should confirm and advance."""
        monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "env-provided-master-key-xyz")
        resp = client.post("/api/setup/auth", json={})
        assert resp.status_code == 200
        body = resp.json()
        auth_step = next(s for s in body["steps"] if s["name"] == "auth")
        assert auth_step["completed"] is True
        assert auth_step["data"]["adopted"] is True
        # Key is preserved, not replaced.
        assert core_auth.JARVIS_API_KEY == "env-provided-master-key-xyz"

    def test_auth_step_confirms_existing_key_when_matching(self, client, monkeypatch):
        existing = "env-provided-master-key-xyz"
        monkeypatch.setattr(core_auth, "JARVIS_API_KEY", existing)
        resp = client.post("/api/setup/auth", json={"api_key": existing})
        assert resp.status_code == 200
        body = resp.json()
        auth_step = next(s for s in body["steps"] if s["name"] == "auth")
        assert auth_step["completed"] is True
        assert auth_step["data"]["adopted"] is True

    def test_auth_probe_reports_configured_state(self, client, monkeypatch):
        monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "")
        resp = client.get("/api/setup/auth/probe")
        assert resp.status_code == 200
        assert resp.json() == {"configured": False}

        monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "something")
        resp = client.get("/api/setup/auth/probe")
        assert resp.json() == {"configured": True}

    def test_auth_probe_is_unauthenticated(self, client, monkeypatch):
        monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "something")
        # No Authorization header.
        resp = client.get("/api/setup/auth/probe")
        assert resp.status_code == 200


# ---- /api/setup/llm ----------------------------------------------------------


class TestLLMStep:
    def test_llm_step_requires_auth(self, client):
        # No master key → verify_api_key returns True (dev mode). Set one first.
        resp = client.post("/api/setup/auth", json={})
        assert resp.status_code == 200

        resp = client.post(
            "/api/setup/llm",
            json={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "api_key": "sk-xxx",
            },
        )
        # Without header — should be 401 now that a key is set.
        assert resp.status_code == 401

    def test_llm_step_happy_path(self, client, db_factory):
        client.post("/api/setup/auth", json={})
        resp = client.post(
            "/api/setup/llm",
            json={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "api_key": "sk-live-xyz",
                "base_url": "https://api.openai.com",
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        llm_step = next(s for s in body["steps"] if s["name"] == "llm")
        assert llm_step["completed"] is True
        # Per-provider schema: the key now lives under the UI-provider's slot
        # (openai → openai_api_key).  ``data`` surfaces a per-slot has-key map
        # and the active-provider shorthand used by the wizard UI.
        data = llm_step["data"]
        assert data["provider"] == "openai"
        assert data["model"] == "gpt-4o-mini"
        assert data["base_url"] == "https://api.openai.com"
        assert data["api_key_set"] is True
        assert data["keys_by_slot"] == {
            "openai": True,
            "anthropic": False,
            "generic": False,
        }

        # api_key must be stored encrypted under the slot-specific key.
        with db_factory() as db:
            row = (
                db.query(SystemConfig)
                .filter_by(category="llm", key="openai_api_key")
                .one()
            )
        assert row.is_secret is True
        assert row.value is not None and not row.value.endswith("sk-live-xyz")


# ---- /api/setup/services -----------------------------------------------------


class TestServicesStep:
    def test_services_step_persists_and_marks_done(self, client, db_factory):
        client.post("/api/setup/auth", json={})
        resp = client.post(
            "/api/setup/services",
            json={
                "services": {
                    "github": {"token": "ghp_xxx"},
                    "openweather": {"api_key": "owm-123"},
                }
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        svc_step = next(s for s in body["steps"] if s["name"] == "services")
        assert svc_step["completed"] is True
        assert sorted(svc_step["data"]["configured"]) == ["github", "openweather"]

        with db_factory() as db:
            gh = (
                db.query(SystemConfig)
                .filter_by(category="service.github", key="token")
                .one()
            )
        assert gh.is_secret is True

    def test_services_step_empty_still_marks_done(self, client):
        client.post("/api/setup/auth", json={})
        resp = client.post(
            "/api/setup/services",
            json={"services": {}},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert next(s for s in body["steps"] if s["name"] == "services")[
            "completed"
        ] is True

    def test_services_step_rejects_bad_name(self, client):
        client.post("/api/setup/auth", json={})
        resp = client.post(
            "/api/setup/services",
            json={"services": {"bad name!": {"k": "v"}}},
            headers=_auth_headers(),
        )
        assert resp.status_code == 400

    def test_jarvis_repo_resolves_immediately_after_wizard(
        self, client, monkeypatch
    ):
        """Wizard → DB → ``get_repo_url()`` works without restart.

        Regression guard: before this PR, ``get_repo_url`` only read
        ``os.environ`` and would either raise or trip the (now-removed)
        git-remote fallback right after a successful wizard write.
        """
        from services import config_service as config_module
        from services import repo_config

        # repo_config bound the singleton at import time — point it at the
        # same instance that the route's ``set_many`` call writes through.
        monkeypatch.setattr(repo_config, "config_service", config_module.config_service)
        # Hard-cut: any ambient env var must not paper over a failure.
        monkeypatch.delenv("JARVIS_REPO_URL", raising=False)

        client.post("/api/setup/auth", json={})
        resp = client.post(
            "/api/setup/services",
            json={
                "services": {
                    "jarvis_repo": {
                        "JARVIS_REPO_URL": "https://github.com/owner/jarvis.git"
                    }
                }
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert repo_config.get_repo_url() == "https://github.com/owner/jarvis.git"

    def test_get_repo_url_raises_when_wizard_skipped(self, client, monkeypatch):
        from services import config_service as config_module
        from services import repo_config

        monkeypatch.setattr(repo_config, "config_service", config_module.config_service)
        monkeypatch.delenv("JARVIS_REPO_URL", raising=False)

        client.post("/api/setup/auth", json={})
        # Wizard runs but doesn't include jarvis_repo.
        client.post(
            "/api/setup/services",
            json={"services": {"github": {"token": "ghp_xxx"}}},
            headers=_auth_headers(),
        )
        with pytest.raises(RuntimeError, match="not configured"):
            repo_config.get_repo_url()


# ---- /api/setup/verify -------------------------------------------------------


class TestVerifyStep:
    def test_verify_fails_without_critical_config(self, client):
        client.post("/api/setup/auth", json={})
        # No LLM configured yet.
        resp = client.post(
            "/api/setup/verify",
            json={"accept_warnings": False},
            headers=_auth_headers(),
        )
        assert resp.status_code == 400

    def test_verify_ok_when_full_stack_present(self, client):
        client.post("/api/setup/auth", json={})
        client.post(
            "/api/setup/llm",
            json={"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-x"},
            headers=_auth_headers(),
        )
        # Skip the optional steps so current_step clears out too.
        client.post("/api/setup/step/services/skip", headers=_auth_headers())
        client.post("/api/setup/step/yaml_config/skip", headers=_auth_headers())
        resp = client.post(
            "/api/setup/verify",
            json={"accept_warnings": False},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["overall_complete"] is True
        assert body["current_step"] is None

    def test_overall_complete_survives_skipped_optional_steps(self, client):
        """Optional steps left un-done do not block `overall_complete`."""
        client.post("/api/setup/auth", json={})
        client.post(
            "/api/setup/llm",
            json={"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-x"},
            headers=_auth_headers(),
        )
        resp = client.post(
            "/api/setup/verify",
            json={"accept_warnings": False},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["overall_complete"] is True
        # current_step still points at the first pending non-critical step.
        assert body["current_step"] == "services"

    def test_verify_with_accept_warnings_bypasses_check(self, client):
        client.post("/api/setup/auth", json={})
        resp = client.post(
            "/api/setup/verify",
            json={"accept_warnings": True},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200


# ---- /api/setup/step/{name}/skip --------------------------------------------


class TestSkip:
    def test_skip_non_critical_step(self, client):
        client.post("/api/setup/auth", json={})
        resp = client.post(
            "/api/setup/step/services/skip", headers=_auth_headers()
        )
        assert resp.status_code == 200
        step = next(
            s for s in resp.json()["steps"] if s["name"] == "services"
        )
        assert step["skipped"] is True
        assert step["completed"] is False

    def test_skip_critical_step_rejected(self, client):
        client.post("/api/setup/auth", json={})
        resp = client.post("/api/setup/step/auth/skip", headers=_auth_headers())
        assert resp.status_code == 400

    def test_skip_unknown_step(self, client):
        client.post("/api/setup/auth", json={})
        resp = client.post(
            "/api/setup/step/nonexistent/skip", headers=_auth_headers()
        )
        assert resp.status_code == 404


# ---- /api/setup/reset --------------------------------------------------------


class TestReset:
    def test_reset_clears_all_flags(self, client, db_factory):
        client.post("/api/setup/auth", json={})
        client.post(
            "/api/setup/step/services/skip", headers=_auth_headers()
        )
        resp = client.post("/api/setup/reset", headers=_auth_headers())
        assert resp.status_code == 200
        for s in resp.json()["steps"]:
            assert s["completed"] is False
            assert s["skipped"] is False
