"""Tests for routes.oauth — Google OAuth web flow."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth as core_auth
from core import secrets_crypto


@pytest.fixture()
def client(tmp_path, monkeypatch):
    key = "oauth-route-tests-master-key"
    monkeypatch.setenv("JARVIS_API_KEY", key)
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", key)
    secrets_crypto.reload_master_key()

    from core.database import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from services.config_service import ConfigService
    from services import google_oauth
    from routes import oauth as oauth_routes

    engine = create_engine(f"sqlite:///{tmp_path}/routes_oauth.db", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    service = ConfigService(db_factory=Session)
    monkeypatch.setattr(google_oauth, "config_service", service)
    # Wipe any leftover pending-state from previous tests.
    oauth_routes._pending_states.clear()

    app = FastAPI()
    app.include_router(oauth_routes.router)
    return TestClient(app), oauth_routes, google_oauth


AUTH = {"Authorization": "Bearer oauth-route-tests-master-key"}


class TestAuth:
    def test_endpoints_require_bearer(self, client):
        tc, _, _ = client
        cases = [
            ("GET", "/api/oauth/google/status", None),
            ("PUT", "/api/oauth/google/client",
                {"client_id": "x", "client_secret": "y", "client_type": "desktop"}),
            ("DELETE", "/api/oauth/google/client", None),
            ("POST", "/api/oauth/google/start", {"redirect_uri": "x"}),
            ("POST", "/api/oauth/google/callback", {"code": "c", "state": "s"}),
            ("DELETE", "/api/oauth/google", None),
        ]
        for method, path, body in cases:
            resp = tc.request(method, path, json=body)
            assert resp.status_code == 401, (method, path)


class TestStatus:
    def test_not_configured(self, client):
        tc, _, _ = client
        resp = tc.get("/api/oauth/google/status", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        # required_apis is always present so the UI can render the enable
        # checklist; links have no ``?project=…`` until a client is saved.
        assert body["client_configured"] is False
        assert body["client_type"] == "none"
        assert body["connected"] is False
        assert body["project_number"] is None
        assert [item["api_id"] for item in body["required_apis"]] == [
            "gmail.googleapis.com",
            "calendar-json.googleapis.com",
        ]
        assert all("?project=" not in item["enable_url"] for item in body["required_apis"])

    def test_project_number_extracted_from_client_id(self, client):
        # Real Google-issued client_ids carry the Cloud project number as the
        # leading digit run. /status parses it and pre-fills enable_url so the
        # user lands on the correct project when they click.
        tc, _, oauth = client
        oauth.save_client(
            "315696637373-abcdef.apps.googleusercontent.com",
            "GOCSPX-dummy",
            "desktop",
        )
        body = tc.get("/api/oauth/google/status", headers=AUTH).json()
        assert body["project_number"] == "315696637373"
        assert all(
            "project=315696637373" in item["enable_url"]
            for item in body["required_apis"]
        )

    def test_project_number_none_for_malformed_client_id(self, client):
        # Legacy/test rows may not follow the ``<number>-<hash>.apps...`` shape
        # (e.g. short IDs used in test fixtures). The endpoint must stay up
        # and return None rather than crashing.
        tc, _, oauth = client
        oauth.save_client("not-a-real-google-id", "secret", "web")
        body = tc.get("/api/oauth/google/status", headers=AUTH).json()
        assert body["project_number"] is None
        assert all("?project=" not in item["enable_url"] for item in body["required_apis"])

    def test_configured_web_and_connected(self, client):
        tc, _, oauth = client
        oauth.save_client("cid", "secret", "web")
        oauth.save_tokens(oauth.GoogleOAuthTokens(
            access_token="AT", refresh_token="RT",
            expires_at=1_700_000_000.0, scopes=("openid",),
            token_uri=oauth.DEFAULT_TOKEN_URI,
        ))
        body = tc.get("/api/oauth/google/status", headers=AUTH).json()
        assert body["client_configured"] is True
        assert body["client_type"] == "web"
        assert body["desktop_redirect_uri"] is None
        assert body["connected"] is True
        assert body["scopes"] == ["openid"]
        assert body["has_refresh_token"] is True

    def test_configured_desktop_exposes_redirect(self, client):
        tc, _, oauth = client
        oauth.save_client("cid", "secret", "desktop")
        body = tc.get("/api/oauth/google/status", headers=AUTH).json()
        assert body["client_type"] == "desktop"
        assert body["desktop_redirect_uri"] == oauth.DESKTOP_LOOPBACK_REDIRECT_URI

    def test_legacy_row_without_client_type_surfaces_as_web(self, client):
        # An existing install from before this refactor has client_id and
        # client_secret in the DB but no client_type row yet. /status must
        # still describe the client as configured (so the UI doesn't force
        # the user back through the paste-creds form) and mark it as "web"
        # — that's what the legacy popup flow was.
        tc, _, oauth = client
        svc = oauth.config_service
        svc.set("oauth.google", "client_id", "legacy-cid", is_secret=True)
        svc.set("oauth.google", "client_secret", "legacy-sec", is_secret=True)
        body = tc.get("/api/oauth/google/status", headers=AUTH).json()
        assert body["client_configured"] is True
        assert body["client_type"] == "web"
        assert body["desktop_redirect_uri"] is None


class TestClientSetter:
    def test_saves_desktop_client(self, client):
        tc, _, oauth = client
        resp = tc.put(
            "/api/oauth/google/client",
            headers=AUTH,
            json={"client_id": "cid", "client_secret": "secret", "client_type": "desktop"},
        )
        assert resp.status_code == 200
        assert oauth.load_client().client_id == "cid"
        assert oauth.client_type() == "desktop"

    def test_saves_web_client(self, client):
        tc, _, oauth = client
        resp = tc.put(
            "/api/oauth/google/client",
            headers=AUTH,
            json={"client_id": "cid", "client_secret": "secret", "client_type": "web"},
        )
        assert resp.status_code == 200
        assert oauth.client_type() == "web"

    def test_rejects_invalid_client_type(self, client):
        tc, _, _ = client
        resp = tc.put(
            "/api/oauth/google/client",
            headers=AUTH,
            json={"client_id": "cid", "client_secret": "secret", "client_type": "installed"},
        )
        assert resp.status_code == 422

    def test_delete_clears_everything(self, client):
        tc, _, oauth = client
        oauth.save_client("db-cid", "db-secret", "desktop")
        resp = tc.delete("/api/oauth/google/client", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["client_type"] == "none"
        assert oauth.load_client() is None


class TestStart:
    def test_requires_client(self, client):
        tc, _, _ = client
        resp = tc.post(
            "/api/oauth/google/start",
            headers=AUTH,
            json={"redirect_uri": "http://x/cb"},
        )
        assert resp.status_code == 400
        assert "not configured" in resp.json()["detail"].lower()

    def test_web_returns_consent_url_and_state(self, client):
        tc, routes, oauth = client
        oauth.save_client("cid", "secret", "web")
        resp = tc.post(
            "/api/oauth/google/start",
            headers=AUTH,
            json={"redirect_uri": "http://x/cb"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["url"].startswith("https://accounts.google.com/")
        assert body["state"] in routes._pending_states
        assert body["client_type"] == "web"
        assert body["redirect_uri"] == "http://x/cb"

    def test_web_without_redirect_uri_rejected(self, client):
        tc, _, oauth = client
        oauth.save_client("cid", "secret", "web")
        resp = tc.post("/api/oauth/google/start", headers=AUTH, json={})
        assert resp.status_code == 400
        assert "redirect_uri" in resp.json()["detail"]

    def test_desktop_forces_loopback_redirect(self, client):
        tc, routes, oauth = client
        oauth.save_client("cid", "secret", "desktop")
        # UI may pass its own origin; desktop mode must ignore it.
        resp = tc.post(
            "/api/oauth/google/start",
            headers=AUTH,
            json={"redirect_uri": "http://attacker.example/cb"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["client_type"] == "desktop"
        assert body["redirect_uri"] == oauth.DESKTOP_LOOPBACK_REDIRECT_URI
        pending_uri, _ = routes._pending_states[body["state"]]
        assert pending_uri == oauth.DESKTOP_LOOPBACK_REDIRECT_URI
        from urllib.parse import quote
        assert quote(oauth.DESKTOP_LOOPBACK_REDIRECT_URI, safe="") in body["url"]


class TestCallback:
    def test_rejects_unknown_state(self, client):
        tc, _, _ = client
        resp = tc.post(
            "/api/oauth/google/callback",
            headers=AUTH,
            json={"code": "c", "state": "never-issued"},
        )
        assert resp.status_code == 400

    def test_exchanges_code_happy_path(self, client, monkeypatch):
        tc, routes, oauth = client
        oauth.save_client("cid", "secret", "web")
        started = tc.post(
            "/api/oauth/google/start",
            headers=AUTH,
            json={"redirect_uri": "http://x/cb"},
        ).json()

        class _Resp:
            ok = True
            status_code = 200
            text = "{}"

            def json(self):
                return {
                    "access_token": "AT",
                    "refresh_token": "RT",
                    "expires_in": 3600,
                    "scope": "openid email",
                }

        monkeypatch.setattr(oauth.requests, "post", lambda *a, **kw: _Resp())

        resp = tc.post(
            "/api/oauth/google/callback",
            headers=AUTH,
            json={"code": "auth-code", "state": started["state"]},
        )
        assert resp.status_code == 200
        assert resp.json()["connected"] is True
        # State is one-shot.
        assert started["state"] not in routes._pending_states
        # Tokens persisted.
        assert oauth.load_tokens().access_token == "AT"

    def test_google_rejection_bubbles_400(self, client, monkeypatch):
        tc, _, oauth = client
        oauth.save_client("cid", "secret", "web")
        started = tc.post(
            "/api/oauth/google/start",
            headers=AUTH,
            json={"redirect_uri": "http://x/cb"},
        ).json()

        class _Resp:
            ok = False
            status_code = 400
            text = "invalid_grant"

            def json(self):
                return {"error": "invalid_grant"}

        monkeypatch.setattr(oauth.requests, "post", lambda *a, **kw: _Resp())
        resp = tc.post(
            "/api/oauth/google/callback",
            headers=AUTH,
            json={"code": "bad", "state": started["state"]},
        )
        assert resp.status_code == 400


class TestDisconnect:
    def test_clears_tokens(self, client, monkeypatch):
        tc, _, oauth = client
        oauth.save_tokens(oauth.GoogleOAuthTokens(
            access_token="AT", refresh_token="RT", expires_at=0,
            scopes=("x",), token_uri=oauth.DEFAULT_TOKEN_URI,
        ))
        # Stub the remote revoke so we don't actually call Google.
        monkeypatch.setattr(oauth.requests, "post", lambda *a, **kw: None)
        resp = tc.delete("/api/oauth/google", headers=AUTH)
        assert resp.status_code == 200
        assert oauth.load_tokens() is None
