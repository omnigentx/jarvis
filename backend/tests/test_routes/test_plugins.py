"""Authenticated API must report pending/unsupported states honestly."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.auth import verify_api_key
from routes.plugins import get_installer, router


@pytest.fixture
def client():
    installer = SimpleNamespace(lifecycle=MagicMock(), install=AsyncMock())
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_installer] = lambda: installer
    app.dependency_overrides[verify_api_key] = lambda: True
    with TestClient(app) as client:
        yield client, installer, app


def test_install_requires_authentication(client):
    browser, installer, app = client

    async def unauthorized():
        raise HTTPException(401, "Unauthorized")

    app.dependency_overrides[verify_api_key] = unauthorized
    assert (
        browser.post(
            "/api/plugins/install", json={"repo": "openai/plugins", "commit": "a" * 40}
        ).status_code
        == 401
    )
    installer.install.assert_not_called()


def test_mutable_revision_is_validation_error(client):
    browser, installer, _ = client
    assert (
        browser.post(
            "/api/plugins/install", json={"repo": "openai/plugins", "commit": "main"}
        ).status_code
        == 422
    )
    installer.install.assert_not_called()


def test_install_reports_source_pending_without_claiming_ready(client):
    browser, installer, _ = client
    installer.install.return_value = {"status": "needs_source_approval"}
    response = browser.post(
        "/api/plugins/install", json={"repo": "openai/plugins", "commit": "a" * 40}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "needs_source_approval"


def test_listing_does_not_expose_server_credentials(client):
    browser, installer, _ = client
    installer.lifecycle.list.return_value = [
        {
            "id": "sample",
            "name": "sample",
            "status": "unsupported",
            "servers": {"remote": {"headers": {"Authorization": "secret"}}},
        }
    ]
    response = browser.get("/api/plugins")
    assert "secret" not in response.text
    assert response.json()["plugins"][0]["server_names"] == ["remote"]


def test_ready_inventory_does_not_survive_missing_runtime(client, monkeypatch):
    from services import shared_state

    monkeypatch.setattr(shared_state, "agent_app", None)
    browser, installer, _ = client
    installer.lifecycle.list.return_value = [
        {
            "id": "sample",
            "name": "sample",
            "status": "ready",
            "skills": [],
            "bindings": [{"agent": "Jarvis", "status": "ready"}],
        }
    ]
    response = browser.get("/api/plugins")
    assert response.json()["plugins"][0]["status"] == "needs_reactivation"
    assert (
        response.json()["plugins"][0]["bindings"][0]["status"] == "needs_reactivation"
    )


def test_download_timeout_is_actionable_and_redacted(client):
    browser, installer, _ = client
    installer.install.side_effect = TimeoutError("private key must not leak")
    response = browser.post(
        "/api/plugins/install", json={"repo": "openai/plugins", "commit": "a" * 40}
    )
    assert response.status_code == 504
    assert "refresh inventory" in response.text
    assert "private key" not in response.text
