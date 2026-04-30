"""End-to-end smoke test for Phase 3 (settings + system + oauth + setup gate).

Mounts the *real* FastAPI app (lifespan + middleware + all routers) against a
temporary SQLite DB.  Exercises the wiring as a whole — no monkeypatching of
individual routers — so this catches things that unit tests can't: router
ordering, middleware interactions, lifespan hooks firing, etc.

Skipped if the env can't bring up the full app (e.g., missing OS deps for
speech_recognition on CI).
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def live_app(tmp_path, monkeypatch):
    """Spin up the real app, but rebind its DB to a tmp path."""
    # Set master key *before* importing server so verify_api_key picks it up.
    master_key = "live-integration-test-master-key"
    monkeypatch.setenv("JARVIS_API_KEY", master_key)
    # server.py has a top-level ``os.environ.setdefault("SPAWN_REGISTRY_DB", ...)``
    # that leaks into subsequent tests once imported.  Register the env var with
    # monkeypatch so it reverts on teardown.
    monkeypatch.delenv("SPAWN_REGISTRY_DB", raising=False)

    # Import lazily so the env-var patches apply.
    from core import auth as core_auth
    from core import database as core_db
    from core import secrets_crypto

    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", master_key)
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None

    db_file = tmp_path / "live.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    core_db.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(core_db, "SessionLocal", Session)
    monkeypatch.setattr(core_db, "engine", engine)

    from services import config_service as cs_module

    monkeypatch.setattr(cs_module, "SessionLocal", Session)
    monkeypatch.setattr(
        cs_module, "config_service", cs_module.ConfigService(db_factory=Session)
    )
    import routes.settings as rs
    import routes.setup as rsetup

    monkeypatch.setattr(rs, "config_service", cs_module.config_service)
    monkeypatch.setattr(rsetup, "config_service", cs_module.config_service)

    # Reset setup-gate cache so the middleware re-reads from our tmp DB.
    from middleware import setup_gate

    setup_gate._reset_cache_for_tests()

    from server import app

    yield TestClient(app), master_key, cs_module.config_service

    engine.dispose()
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None
    setup_gate._reset_cache_for_tests()
    # server.py's top-level os.environ.setdefault leaks SPAWN_REGISTRY_DB.
    # monkeypatch can't auto-revert it since it wasn't monkeypatch that set it.
    os.environ.pop("SPAWN_REGISTRY_DB", None)


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class TestSetupGate:
    def test_blocks_settings_until_setup_complete(self, live_app):
        client, key, _ = live_app
        resp = client.get("/api/settings", headers=_h(key))
        assert resp.status_code == 503
        assert resp.headers.get("X-Setup-Required") == "true"

    def test_setup_status_always_reachable(self, live_app):
        client, key, _ = live_app
        resp = client.get("/api/setup/status", headers=_h(key))
        assert resp.status_code == 200


def _complete_setup(client, key, svc):
    """Shortcut: mark all critical wizard steps complete via the DB."""
    from core.database import SETUP_WIZARD_CRITICAL_STEPS, SetupWizardStep, get_db_session

    db = get_db_session()
    try:
        for step in SETUP_WIZARD_CRITICAL_STEPS:
            row = SetupWizardStep(step_name=step, completed=True)
            db.merge(row)
        db.commit()
    finally:
        db.close()
    from middleware import setup_gate

    setup_gate.refresh_setup_complete()


class TestExportImportRoundtrip:
    def test_export_import_merge_preserves_state(self, live_app):
        client, key, svc = live_app
        _complete_setup(client, key, svc)

        # Seed a few entries.
        svc.set("llm", "model", "gpt-4o")
        svc.set("llm", "provider", "openai")
        svc.set("auth", "token", "tok-abc", is_secret=True)

        # Export — secrets masked by default.
        resp = client.get("/api/settings/export", headers=_h(key))
        assert resp.status_code == 200
        export = resp.json()
        assert export["version"] == 1
        items = {(i["category"], i["key"]): i for i in export["items"]}
        assert items[("auth", "token")]["value"] == "__SECRET__"
        assert items[("llm", "model")]["value"] == "gpt-4o"

        # Mutate: change model.
        svc.set("llm", "model", "gpt-3.5")
        assert svc.get("llm", "model") == "gpt-3.5"

        # Re-import (merge) — should restore gpt-4o but skip __SECRET__.
        resp = client.post("/api/settings/import", json=export, headers=_h(key))
        assert resp.status_code == 200
        body = resp.json()
        assert body["applied"] >= 2
        assert body["skipped_secrets"] == ["auth/token"]
        assert svc.get("llm", "model") == "gpt-4o"
        # Secret wasn't overwritten with placeholder.
        assert svc.get("auth", "token") == "tok-abc"

    def test_export_with_secrets_roundtrips(self, live_app):
        client, key, svc = live_app
        _complete_setup(client, key, svc)
        svc.set("auth", "token", "real-tok", is_secret=True)

        # Plaintext export.
        resp = client.get(
            "/api/settings/export?include_secrets=true", headers=_h(key)
        )
        export = resp.json()
        assert export["includes_secrets"] is True
        tok = next(i for i in export["items"] if i["key"] == "token")
        assert tok["value"] == "real-tok"

        # Wipe the real value, then re-import → should come back.
        svc.delete("auth", "token")
        assert svc.get("auth", "token") is None

        resp = client.post("/api/settings/import", json=export, headers=_h(key))
        assert resp.status_code == 200
        assert svc.get("auth", "token") == "real-tok"


class TestSystemRestart:
    def test_restart_endpoint_returns_pid(self, live_app, monkeypatch):
        client, key, _ = live_app
        _complete_setup(client, key, _)

        # Intercept os.kill so the background SIGTERM can't land on our test
        # runner if the task actually fires.
        import routes.system as system_routes

        monkeypatch.setattr(system_routes.os, "kill", lambda p, s: None)
        # Zero the delay just in case.
        monkeypatch.setattr(
            system_routes, "_trigger_exit", lambda _d: asyncio.sleep(0)
        )

        resp = client.post("/api/system/restart", headers=_h(key))
        assert resp.status_code == 200
        body = resp.json()
        assert body["restarting"] is True
        assert body["pid"] == os.getpid()

    def test_restart_requires_auth(self, live_app):
        client, key, _ = live_app
        _complete_setup(client, key, _)
        resp = client.post("/api/system/restart")
        assert resp.status_code == 401


class TestOAuthStatus:
    def test_status_reports_no_client_when_unset(self, live_app):
        client, key, _ = live_app
        _complete_setup(client, key, _)

        resp = client.get("/api/oauth/google/status", headers=_h(key))
        assert resp.status_code == 200
        body = resp.json()
        assert body["client_configured"] is False
        assert body["connected"] is False


class TestHistoryAfterImport:
    def test_import_creates_history_rows(self, live_app):
        client, key, svc = live_app
        _complete_setup(client, key, svc)
        resp = client.post(
            "/api/settings/import",
            headers=_h(key),
            json={
                "version": 1,
                "items": [
                    {
                        "category": "llm",
                        "key": "model",
                        "value": "gpt-4o",
                        "is_secret": False,
                    }
                ],
            },
        )
        assert resp.status_code == 200
        hist = client.get(
            "/api/settings/history?category=llm&key=model", headers=_h(key)
        )
        assert hist.status_code == 200
        items = hist.json()["items"]
        assert len(items) >= 1
        assert items[0]["new_value"] == "gpt-4o"
