"""Tests for middleware.setup_gate — 503 gate while wizard incomplete."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import auth as core_auth
from core import secrets_crypto
from core.database import Base, SETUP_WIZARD_STEPS, SetupWizardStep
from middleware.setup_gate import (
    SetupGateMiddleware,
    _reset_cache_for_tests,
    refresh_setup_complete,
)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", "gate-tests-master-key-123")
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "gate-tests-master-key-123")
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None
    _reset_cache_for_tests()
    yield
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None
    _reset_cache_for_tests()


@pytest.fixture()
def db_factory(tmp_path, monkeypatch):
    db_file = tmp_path / "gate_test.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with SessionFactory() as db:
        for name in SETUP_WIZARD_STEPS:
            db.add(SetupWizardStep(step_name=name))
        db.commit()

    import core.database as core_db
    from services import config_service as config_module

    monkeypatch.setattr(core_db, "SessionLocal", SessionFactory)
    monkeypatch.setattr(config_module, "SessionLocal", SessionFactory)
    monkeypatch.setattr(
        config_module,
        "config_service",
        config_module.ConfigService(db_factory=SessionFactory),
    )
    import routes.setup as setup_routes
    import routes.settings as settings_routes

    monkeypatch.setattr(setup_routes, "config_service", config_module.config_service)
    monkeypatch.setattr(settings_routes, "config_service", config_module.config_service)

    # Also swap the module-level session factory that setup_gate uses via
    # ``get_db_session`` — that function is already wired through core.database,
    # so patching SessionLocal is enough.
    yield SessionFactory
    engine.dispose()


def _build_app() -> FastAPI:
    from routes.settings import router as settings_router
    from routes.setup import router as setup_router

    app = FastAPI()
    app.add_middleware(SetupGateMiddleware)
    app.include_router(setup_router)
    app.include_router(settings_router)

    # Extra dummy API route so we can exercise a non-bootstrap endpoint.
    @app.get("/api/dummy")
    def dummy():
        return {"ok": True}

    # Stand-in for /api/oauth/* — we don't need the real Google route to
    # assert the gate behaviour; we just need a path under the prefix to
    # confirm the middleware lets it through.
    @app.get("/api/oauth/google/status")
    def oauth_status_stub():
        return {
            "client_configured": False,
            "client_type": "none",
            "connected": False,
            "required_apis": [],
        }

    @app.get("/")
    def root():
        return {"ui": True}

    return app


@pytest.fixture()
def client(db_factory):
    refresh_setup_complete()
    return TestClient(_build_app())


# ---- Gate behaviour ----------------------------------------------------------


class TestGateClosed:
    def test_dummy_api_blocked_before_setup(self, client):
        resp = client.get("/api/dummy")
        assert resp.status_code == 503
        assert resp.headers.get("X-Setup-Required") == "true"
        body = resp.json()
        assert body["error"] == "setup_required"
        assert body["redirect"] == "/#/setup"

    def test_settings_api_blocked_before_setup(self, client):
        resp = client.get(
            "/api/settings", headers={"Authorization": "Bearer gate-tests-master-key-123"}
        )
        assert resp.status_code == 503

    def test_setup_status_always_allowed(self, client):
        resp = client.get(
            "/api/setup/status", headers={"Authorization": "Bearer gate-tests-master-key-123"}
        )
        assert resp.status_code == 200

    def test_auth_routes_always_allowed(self, client):
        # Even though login will reject without a real key, the gate must not
        # intercept.  We see a non-503 status code.
        resp = client.post("/api/auth/login", json={"password": "x"})
        assert resp.status_code != 503

    def test_openapi_always_allowed(self, client):
        assert client.get("/openapi.json").status_code == 200

    def test_oauth_routes_allowed_during_setup(self, client):
        # Wizard Step 3 (External Services) calls /api/oauth/google/status
        # to render the Connect Google button. This must succeed even
        # though the wizard hasn't completed yet — otherwise the user sees
        # the 503 setup_required error mid-wizard. Regression guard for
        # the bug where /api/oauth was missing from _ALLOWED_PREFIXES and
        # the wizard's Google card surfaced "Jarvis is not configured yet".
        resp = client.get(
            "/api/oauth/google/status",
            headers={"Authorization": "Bearer gate-tests-master-key-123"},
        )
        assert resp.status_code == 200, (
            "Setup gate must allow /api/oauth/* — wizard Step 3 needs it"
        )
        assert resp.json()["client_type"] == "none"

    def test_non_api_paths_always_allowed(self, client):
        # The SPA root ("/") must render so the user can see the wizard UI.
        assert client.get("/").status_code == 200


# ---- Gate opens after setup --------------------------------------------------


class TestGateOpens:
    def _complete_wizard(self, client):
        headers = {"Authorization": "Bearer gate-tests-master-key-123"}
        # Already past auth step (master key is set in env). Bypass the auth
        # endpoint by marking the step directly via /skip won't work (critical).
        # Instead, write data via setup routes; the /auth endpoint refuses when
        # a key is already set, so we mark the auth step by going through the
        # wizard table directly.
        from core.database import SetupWizardStep, get_db_session

        db = get_db_session()
        try:
            for name in ("auth", "llm", "verify"):
                row = (
                    db.query(SetupWizardStep)
                    .filter_by(step_name=name)
                    .one_or_none()
                )
                if row is None:
                    row = SetupWizardStep(step_name=name, completed=True)
                    db.add(row)
                else:
                    row.completed = True
            db.commit()
        finally:
            db.close()
        refresh_setup_complete()

    def test_api_accessible_after_critical_steps_done(self, client):
        self._complete_wizard(client)
        resp = client.get("/api/dummy")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_wizard_mutation_invalidates_cache(self, client, db_factory):
        # Force cache to think wizard is done.
        from core.database import SetupWizardStep, get_db_session

        db = get_db_session()
        try:
            for name in ("auth", "llm", "verify"):
                row = db.query(SetupWizardStep).filter_by(step_name=name).one()
                row.completed = True
            db.commit()
        finally:
            db.close()
        refresh_setup_complete()
        assert client.get("/api/dummy").status_code == 200

        # Now call /api/setup/reset — this should flip the cache closed.
        headers = {"Authorization": "Bearer gate-tests-master-key-123"}
        resp = client.post("/api/setup/reset", headers=headers)
        assert resp.status_code == 200
        assert client.get("/api/dummy").status_code == 503
