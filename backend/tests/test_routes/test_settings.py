"""Tests for routes/settings.py — Settings HTTP endpoints."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import auth as core_auth
from core import secrets_crypto
from core.database import Base


# ---- Fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", "unit-test-master-key-abc123")
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "unit-test-master-key-abc123")
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None
    yield
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None


@pytest.fixture()
def db_factory(tmp_path, monkeypatch):
    db_file = tmp_path / "settings_test.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    import core.database as core_db
    from services import config_service as config_module

    monkeypatch.setattr(core_db, "SessionLocal", SessionFactory)
    monkeypatch.setattr(config_module, "SessionLocal", SessionFactory)
    monkeypatch.setattr(
        config_module,
        "config_service",
        config_module.ConfigService(db_factory=SessionFactory),
    )
    import routes.settings as settings_routes

    monkeypatch.setattr(settings_routes, "config_service", config_module.config_service)
    yield SessionFactory
    engine.dispose()


@pytest.fixture()
def svc(db_factory):
    """Direct handle to the patched ConfigService singleton."""
    from services import config_service as config_module

    return config_module.config_service


@pytest.fixture()
def client(db_factory):
    from routes.settings import router as settings_router

    app = FastAPI()
    app.include_router(settings_router)
    return TestClient(app)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {core_auth.JARVIS_API_KEY}"}


# ---- Auth --------------------------------------------------------------------


class TestAuth:
    def test_list_rejects_missing_bearer(self, client):
        assert client.get("/api/settings").status_code == 401

    def test_list_rejects_wrong_bearer(self, client):
        resp = client.get(
            "/api/settings", headers={"Authorization": "Bearer wrong-key"}
        )
        assert resp.status_code == 401


# ---- GET endpoints -----------------------------------------------------------


class TestReads:
    def test_list_all_empty(self, client):
        resp = client.get("/api/settings", headers=_headers())
        assert resp.status_code == 200
        assert resp.json() == {"categories": {}}

    def test_list_all_groups_and_masks(self, client, svc):
        svc.set("llm", "model", "gpt-4o")
        svc.set("auth", "secret_key", "s3cr3t", is_secret=True)
        resp = client.get("/api/settings", headers=_headers())
        body = resp.json()
        assert set(body["categories"].keys()) == {"llm", "auth"}
        sec = body["categories"]["auth"][0]
        assert sec["value"] == "***"
        assert sec["has_value"] is True
        plain = body["categories"]["llm"][0]
        assert plain["value"] == "gpt-4o"

    def test_list_category(self, client, svc):
        svc.set("llm", "model", "gpt-4o")
        resp = client.get("/api/settings/llm", headers=_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["category"] == "llm"
        assert body["items"][0]["key"] == "model"

    def test_get_entry_missing(self, client):
        resp = client.get("/api/settings/llm/nope", headers=_headers())
        assert resp.status_code == 404

    def test_get_entry_masks_secret(self, client, svc):
        svc.set("auth", "api_key", "sk-xyz", is_secret=True)
        resp = client.get("/api/settings/auth/api_key", headers=_headers())
        body = resp.json()
        assert body["value"] == "***"
        assert body["is_secret"] is True


# ---- PUT endpoint ------------------------------------------------------------


class TestPut:
    def test_put_creates(self, client, svc):
        resp = client.put(
            "/api/settings/llm/model",
            json={"value": "gpt-4o", "is_secret": False},
            headers=_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "create"
        assert svc.get("llm", "model") == "gpt-4o"

    def test_put_updates(self, client, svc):
        svc.set("llm", "model", "gpt-3.5")
        resp = client.put(
            "/api/settings/llm/model",
            json={"value": "gpt-4o", "is_secret": False},
            headers=_headers(),
        )
        assert resp.json()["action"] == "update"
        assert svc.get("llm", "model") == "gpt-4o"

    def test_put_encrypts_secret(self, client, svc, db_factory):
        from core.database import SystemConfig

        resp = client.put(
            "/api/settings/auth/token",
            json={"value": "tok-abc", "is_secret": True},
            headers=_headers(),
        )
        assert resp.status_code == 200
        with db_factory() as db:
            row = (
                db.query(SystemConfig)
                .filter_by(category="auth", key="token")
                .one()
            )
        assert row.is_secret is True
        assert row.value != "tok-abc"
        assert row.value.startswith("v1:")

    def test_put_master_key_propagates(self, client):
        resp = client.put(
            "/api/settings/auth/JARVIS_API_KEY",
            json={"value": "brand-new-master-key-999", "is_secret": False},
            headers=_headers(),
        )
        assert resp.status_code == 200
        assert core_auth.JARVIS_API_KEY == "brand-new-master-key-999"

    def test_put_validates_identifiers(self, client):
        resp = client.put(
            "/api/settings/llm/  ",
            json={"value": "x", "is_secret": False},
            headers=_headers(),
        )
        # ConfigService validation raises ValueError → 400
        assert resp.status_code == 400


# ---- DELETE endpoint ---------------------------------------------------------


class TestDelete:
    def test_delete_hits(self, client, svc):
        svc.set("llm", "model", "gpt-4o")
        resp = client.delete("/api/settings/llm/model", headers=_headers())
        assert resp.status_code == 200
        assert svc.get_entry("llm", "model") is None

    def test_delete_missing_returns_404(self, client):
        resp = client.delete("/api/settings/llm/nope", headers=_headers())
        assert resp.status_code == 404

    def test_delete_master_key_refused(self, client, svc):
        svc.set("auth", "JARVIS_API_KEY", "some-key")
        resp = client.delete(
            "/api/settings/auth/JARVIS_API_KEY", headers=_headers()
        )
        assert resp.status_code == 400


# ---- Bulk --------------------------------------------------------------------


class TestBulk:
    def test_bulk_commits_all(self, client, svc):
        resp = client.post(
            "/api/settings/bulk",
            json={
                "items": [
                    {
                        "category": "llm",
                        "key": "model",
                        "value": "gpt-4o",
                        "is_secret": False,
                    },
                    {
                        "category": "auth",
                        "key": "token",
                        "value": "tok-123",
                        "is_secret": True,
                    },
                ]
            },
            headers=_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["events"]) == 2
        assert svc.get("auth", "token") == "tok-123"

    def test_bulk_empty_rejected_by_schema(self, client):
        resp = client.post(
            "/api/settings/bulk", json={"items": []}, headers=_headers()
        )
        # Pydantic min_length=1 → 422
        assert resp.status_code == 422

    def test_bulk_master_key_propagates(self, client):
        resp = client.post(
            "/api/settings/bulk",
            json={
                "items": [
                    {
                        "category": "auth",
                        "key": "JARVIS_API_KEY",
                        "value": "bulk-rotated-master-key",
                        "is_secret": False,
                    }
                ]
            },
            headers=_headers(),
        )
        assert resp.status_code == 200
        assert core_auth.JARVIS_API_KEY == "bulk-rotated-master-key"


# ---- History -----------------------------------------------------------------


class TestHistory:
    def test_history_filters(self, client, svc):
        svc.set("llm", "model", "gpt-3.5")
        svc.set("llm", "model", "gpt-4o")
        svc.set("llm", "temperature", "0.7")
        resp = client.get(
            "/api/settings/history?category=llm&key=model", headers=_headers()
        )
        body = resp.json()
        assert len(body["items"]) == 2
        for it in body["items"]:
            assert it["category"] == "llm"
            assert it["key"] == "model"

    def test_history_limit_clamp(self, client):
        resp = client.get(
            "/api/settings/history?limit=600", headers=_headers()
        )
        # limit > 500 → 422 from Pydantic Query validator
        assert resp.status_code == 422

    def test_history_masks_secrets(self, client, svc):
        svc.set("auth", "api_key", "sk-original", is_secret=True)
        svc.set("auth", "api_key", "sk-rotated", is_secret=True)
        resp = client.get(
            "/api/settings/history?category=auth&key=api_key",
            headers=_headers(),
        )
        body = resp.json()
        assert len(body["items"]) == 2
        for it in body["items"]:
            if it["old_value"]:
                assert it["old_value"] == "***"
            if it["new_value"]:
                assert it["new_value"] == "***"


# ---- Export ------------------------------------------------------------------


class TestExport:
    def test_export_masks_secrets_by_default(self, client, svc):
        svc.set("llm", "model", "gpt-4o")
        svc.set("auth", "token", "tok-abc", is_secret=True)
        resp = client.get("/api/settings/export", headers=_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == 1
        assert body["includes_secrets"] is False
        items = {(i["category"], i["key"]): i for i in body["items"]}
        assert items[("llm", "model")]["value"] == "gpt-4o"
        sec = items[("auth", "token")]
        assert sec["value"] == "__SECRET__"
        assert sec["is_secret"] is True

    def test_export_includes_plain_secrets_when_opted_in(self, client, svc):
        svc.set("auth", "token", "tok-abc", is_secret=True)
        resp = client.get(
            "/api/settings/export?include_secrets=true", headers=_headers()
        )
        body = resp.json()
        assert body["includes_secrets"] is True
        sec = next(i for i in body["items"] if i["key"] == "token")
        assert sec["value"] == "tok-abc"

    def test_export_requires_auth(self, client):
        assert client.get("/api/settings/export").status_code == 401


# ---- Import ------------------------------------------------------------------


class TestImport:
    def test_import_applies_items(self, client, svc):
        resp = client.post(
            "/api/settings/import",
            json={
                "version": 1,
                "items": [
                    {
                        "category": "llm",
                        "key": "model",
                        "value": "gpt-4o",
                        "is_secret": False,
                    },
                    {
                        "category": "auth",
                        "key": "token",
                        "value": "tok-abc",
                        "is_secret": True,
                    },
                ],
            },
            headers=_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["applied"] == 2
        assert body["deleted"] == 0
        assert body["skipped_secrets"] == []
        assert svc.get("llm", "model") == "gpt-4o"
        assert svc.get("auth", "token") == "tok-abc"

    def test_import_skips_secret_placeholders(self, client, svc):
        svc.set("auth", "token", "real-secret", is_secret=True)
        resp = client.post(
            "/api/settings/import",
            json={
                "version": 1,
                "items": [
                    {
                        "category": "auth",
                        "key": "token",
                        "value": "__SECRET__",
                        "is_secret": True,
                    },
                    {
                        "category": "llm",
                        "key": "model",
                        "value": "gpt-4o",
                        "is_secret": False,
                    },
                ],
            },
            headers=_headers(),
        )
        body = resp.json()
        assert body["applied"] == 1
        assert body["skipped_secrets"] == ["auth/token"]
        assert svc.get("auth", "token") == "real-secret"
        assert svc.get("llm", "model") == "gpt-4o"

    def test_import_replace_deletes_missing_keys(self, client, svc):
        svc.set("llm", "model", "gpt-3.5")
        svc.set("llm", "temperature", "0.7")
        resp = client.post(
            "/api/settings/import",
            json={
                "version": 1,
                "replace": True,
                "items": [
                    {
                        "category": "llm",
                        "key": "model",
                        "value": "gpt-4o",
                        "is_secret": False,
                    }
                ],
            },
            headers=_headers(),
        )
        body = resp.json()
        assert body["applied"] == 1
        assert body["deleted"] == 1
        assert svc.get("llm", "model") == "gpt-4o"
        assert svc.get_entry("llm", "temperature") is None

    def test_import_merge_preserves_untouched_keys(self, client, svc):
        svc.set("llm", "model", "gpt-3.5")
        svc.set("llm", "temperature", "0.7")
        resp = client.post(
            "/api/settings/import",
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
            headers=_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 0
        assert svc.get("llm", "temperature") == "0.7"

    def test_import_rejects_unknown_version(self, client):
        resp = client.post(
            "/api/settings/import",
            json={
                "version": 999,
                "items": [
                    {
                        "category": "llm",
                        "key": "model",
                        "value": "gpt-4o",
                        "is_secret": False,
                    }
                ],
            },
            headers=_headers(),
        )
        # ge=1 le=99 → Pydantic rejects
        assert resp.status_code == 422

    def test_import_master_key_propagates(self, client):
        resp = client.post(
            "/api/settings/import",
            json={
                "version": 1,
                "items": [
                    {
                        "category": "auth",
                        "key": "JARVIS_API_KEY",
                        "value": "imported-master-key",
                        "is_secret": False,
                    }
                ],
            },
            headers=_headers(),
        )
        assert resp.status_code == 200
        assert core_auth.JARVIS_API_KEY == "imported-master-key"

    def test_import_requires_auth(self, client):
        resp = client.post(
            "/api/settings/import",
            json={
                "version": 1,
                "items": [
                    {
                        "category": "llm",
                        "key": "model",
                        "value": "x",
                        "is_secret": False,
                    }
                ],
            },
        )
        assert resp.status_code == 401
