"""Tests for routes.sessions — chat conversation HTTP API.

Covers the create + delete endpoints together so creation tests don't ship
without their cleanup-path counterparts (= future test runs of the create
case would otherwise pile up zombie sessions in the dev environment).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth as core_auth
from fast_agent.session import SessionManager
from routes import sessions as sessions_routes
from services import shared_state
from services.session_service import SessionService


_KEY = "session-route-tests-key"
AUTH = {"Authorization": f"Bearer {_KEY}"}


@pytest.fixture(autouse=True)
def _set_master_key(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", _KEY)
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", _KEY)


@pytest.fixture()
def isolated_session_service(tmp_path, monkeypatch):
    """Replace the global session_service with one rooted at tmp_path so
    these tests don't leak into the real ``.fast-agent/sessions/`` dir.
    """
    svc = SessionService()
    svc._manager = SessionManager(cwd=tmp_path)
    monkeypatch.setattr(shared_state, "session_service", svc)
    monkeypatch.setattr(sessions_routes, "session_service", svc)
    return svc


@pytest.fixture()
def client(isolated_session_service):
    app = FastAPI()
    app.include_router(sessions_routes.router)
    return TestClient(app)


class TestCreateAndDelete:
    def test_create_then_delete_round_trip(self, client):
        # Create.
        r1 = client.post("/api/conversations", headers=AUTH, json={"title": "Throwaway"})
        assert r1.status_code == 200
        body = r1.json()
        assert body["title"] == "Throwaway"
        sid = body["id"]
        assert sid

        # Delete.
        r2 = client.delete(f"/api/conversations/{sid}", headers=AUTH)
        assert r2.status_code == 200
        assert r2.json() == {"status": "deleted", "id": sid}

    def test_delete_unknown_id_still_200(self, client):
        # The route always returns 200 — the service layer signals
        # "wasn't there" via boolean, not HTTP. That's intentional so the
        # frontend's delete button is idempotent (refreshing the list
        # right before clicking can leave it pointing at a now-gone id).
        r = client.delete("/api/conversations/does-not-exist", headers=AUTH)
        assert r.status_code == 200

    def test_create_requires_auth(self, client):
        assert client.post("/api/conversations", json={"title": "x"}).status_code == 401

    def test_delete_requires_auth(self, client):
        assert client.delete("/api/conversations/anything").status_code == 401

    def test_create_default_title_when_body_missing(self, client):
        # POST with no body → service falls back to "New Chat".
        r = client.post("/api/conversations", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["title"] == "New Chat"


def _seed(svc, title, agent):
    """Create a *listable* session (one with a stamped primary agent)."""
    from services.session_service import PRIMARY_AGENT_META_KEY
    s = svc._manager.create_session(
        metadata={"title": title, PRIMARY_AGENT_META_KEY: agent}
    )
    return s.info.name


class TestListConversations:
    def test_returns_envelope(self, client):
        r = client.get("/api/conversations", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"items", "total"}

    def test_filters_by_agent(self, client, isolated_session_service):
        _seed(isolated_session_service, "J1", "Jarvis")
        _seed(isolated_session_service, "I1", "IoT")

        r = client.get("/api/conversations", params={"agent_name": "Jarvis"}, headers=AUTH)
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["agent_name"] == "Jarvis"

    def test_pagination(self, client, isolated_session_service):
        for i in range(3):
            _seed(isolated_session_service, f"C{i}", "Jarvis")
        r = client.get(
            "/api/conversations",
            params={"agent_name": "Jarvis", "limit": 2, "offset": 0},
            headers=AUTH,
        )
        body = r.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2

    def test_requires_auth(self, client):
        assert client.get("/api/conversations").status_code == 401


class TestBulkDelete:
    def test_deletes_many_and_reports_partial_failures(self, client, isolated_session_service):
        a = _seed(isolated_session_service, "A", "Jarvis")
        b = _seed(isolated_session_service, "B", "Jarvis")
        r = client.post(
            "/api/conversations/bulk-delete",
            headers=AUTH,
            json={"ids": [a, b, "ghost-id"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert set(body["deleted"]) == {a, b}
        assert body["failed"] == ["ghost-id"]
        # The two real sessions are gone from the manager.
        remaining = {info.name for info in isolated_session_service._manager.list_sessions()}
        assert a not in remaining and b not in remaining

    def test_empty_ids_rejected(self, client):
        r = client.post("/api/conversations/bulk-delete", headers=AUTH, json={"ids": []})
        assert r.status_code == 422

    def test_requires_auth(self, client):
        r = client.post("/api/conversations/bulk-delete", json={"ids": ["x"]})
        assert r.status_code == 401
