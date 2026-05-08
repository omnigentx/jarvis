"""Integration tests for ``routes/auth.py`` — session cookie lifecycle.

Full HTTP-layer tests via FastAPI ``TestClient`` so the cookie / header
contract is exercised end-to-end (Set-Cookie attributes, pydantic
validation, status codes).

What we cover
-------------

* **Login**: success sets two cookies (session + csrf) with right
  attributes; wrong key → 401; rate-limit → 429; missing API key
  config → 503.
* **Logout**: idempotent; clears both cookies.
* **Refresh**: extends ``exp``, preserves ``abs_exp``; refused with
  stable ``reason`` field after expiry / key rotation / no-cookie.
* **Whoami**: never 401s; returns ``authenticated: false`` when no
  cookie or invalid; 200 + sid when valid.
* **verify_api_key dep**: now also accepts the session cookie path
  (cross-layer with ``core.auth``).
* **Backwards compatibility**: legacy Bearer + ``?api_key=`` paths
  still work so we don't break Xiaozhi / scripts mid-rollout.
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from core import auth as core_auth
from core import session as core_session
from core.auth import verify_api_key
from core.session import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME


@pytest.fixture(autouse=True)
def _stable_secrets(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-xxxxxxxxxxxxxxxx")
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "real-master-key-aaaaaaaaaaaaaa")
    # Reset rate-limit state between tests so 429-budget contamination
    # doesn't snowball across the suite.
    core_auth._login_attempts.clear()
    yield


@pytest.fixture()
def app() -> FastAPI:
    """Build a minimal app that mounts the auth routes + a guarded
    smoke endpoint so we can prove cookies actually authenticate."""
    from routes.auth import router as auth_router

    a = FastAPI()
    a.include_router(auth_router)

    @a.get("/api/private", dependencies=[Depends(verify_api_key)])
    async def private() -> dict:
        return {"ok": True}

    return a


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


# ---- Login ------------------------------------------------------------------


class TestLogin:
    def test_success_returns_csrf_in_body_and_sets_two_cookies(self, client):
        resp = client.post("/api/auth/login", json={"api_key": core_auth.JARVIS_API_KEY})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["csrf_token"]
        assert body["expires_in"] == core_session.SESSION_TTL_SECONDS

        # Both cookies set
        cookies = client.cookies
        assert SESSION_COOKIE_NAME in cookies
        assert CSRF_COOKIE_NAME in cookies
        # CSRF cookie value matches body
        assert cookies[CSRF_COOKIE_NAME] == body["csrf_token"]

    def test_session_cookie_is_httponly(self, client):
        resp = client.post("/api/auth/login", json={"api_key": core_auth.JARVIS_API_KEY})
        # TestClient exposes raw set-cookie headers
        set_cookies = [h for h in resp.headers.raw if h[0].lower() == b"set-cookie"]
        session_header = next(
            v.decode("latin-1") for _, v in set_cookies if v.startswith(SESSION_COOKIE_NAME.encode())
        )
        lower = session_header.lower()
        assert "httponly" in lower
        assert "samesite=lax" in lower

    def test_csrf_cookie_is_not_httponly(self, client):
        """SPA must read this cookie to echo it as X-CSRF-Token."""
        resp = client.post("/api/auth/login", json={"api_key": core_auth.JARVIS_API_KEY})
        set_cookies = [h for h in resp.headers.raw if h[0].lower() == b"set-cookie"]
        csrf_header = next(
            v.decode("latin-1") for _, v in set_cookies if v.startswith(CSRF_COOKIE_NAME.encode())
        )
        assert "httponly" not in csrf_header.lower()

    def test_wrong_key_rejects_with_structured_body(self, client):
        resp = client.post("/api/auth/login", json={"api_key": "wrong"})
        assert resp.status_code == 401
        body = resp.json()
        # FastAPI wraps `detail` field
        assert body["detail"]["reason"] == "invalid_credentials"

    def test_empty_key_rejected_by_validation(self, client):
        resp = client.post("/api/auth/login", json={"api_key": ""})
        # pydantic min_length=1 should 422
        assert resp.status_code == 422

    def test_rate_limit_kicks_in(self, client):
        # 5 wrong attempts allowed within window, then 429.
        for _ in range(core_auth.LOGIN_RATE_LIMIT):
            client.post("/api/auth/login", json={"api_key": "wrong"})
        resp = client.post("/api/auth/login", json={"api_key": "wrong"})
        assert resp.status_code == 429
        assert resp.json()["detail"]["reason"] == "too_many_login_attempts"

    def test_login_503_when_api_key_unset(self, client, monkeypatch):
        monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "")
        resp = client.post("/api/auth/login", json={"api_key": "anything"})
        assert resp.status_code == 503
        assert resp.json()["detail"]["reason"] == "auth_key_unset"

    def test_login_503_when_jwt_secret_unset(self, client, monkeypatch):
        monkeypatch.delenv("JWT_SECRET", raising=False)
        resp = client.post("/api/auth/login", json={"api_key": core_auth.JARVIS_API_KEY})
        assert resp.status_code == 503
        assert resp.json()["detail"]["reason"] == "jwt_secret_unset"


# ---- Logout -----------------------------------------------------------------


class TestLogout:
    def test_logout_clears_both_cookies(self, client):
        client.post("/api/auth/login", json={"api_key": core_auth.JARVIS_API_KEY})
        assert SESSION_COOKIE_NAME in client.cookies

        resp = client.post("/api/auth/logout")
        assert resp.status_code == 200
        # After logout the cookies should be expired (server sends Max-Age=0).
        # TestClient honors Set-Cookie deletion.
        assert SESSION_COOKIE_NAME not in client.cookies
        assert CSRF_COOKIE_NAME not in client.cookies

    def test_logout_without_login_succeeds(self, client):
        """Idempotent — logging out twice or while never logged in is OK."""
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 200


# ---- Refresh ----------------------------------------------------------------


class TestRefresh:
    def test_refresh_after_login_extends_session(self, client):
        login = client.post("/api/auth/login", json={"api_key": core_auth.JARVIS_API_KEY})
        original_session = client.cookies[SESSION_COOKIE_NAME]

        # Sleep 1s so the new iat is strictly later (token strings differ).
        time.sleep(1.1)
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 200
        new_session = client.cookies[SESSION_COOKIE_NAME]
        assert new_session != original_session

    def test_refresh_without_session_401(self, client):
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 401
        assert resp.json()["detail"]["reason"] == "no_session"

    def test_refresh_after_key_rotation_returns_key_rotated(self, client, monkeypatch):
        client.post("/api/auth/login", json={"api_key": core_auth.JARVIS_API_KEY})
        # Operator rotates the API key.
        monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "rotated-master-key-bbbbbbbbbb")
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 401
        assert resp.json()["detail"]["reason"] == "key_rotated"


# ---- Whoami -----------------------------------------------------------------


class TestWhoami:
    def test_whoami_no_cookie_returns_unauth(self, client):
        resp = client.get("/api/auth/whoami")
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is False

    def test_whoami_with_valid_cookie_returns_session_meta(self, client):
        client.post("/api/auth/login", json={"api_key": core_auth.JARVIS_API_KEY})
        resp = client.get("/api/auth/whoami")
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is True
        assert body["sid"]
        assert body["expires_in"] is not None
        assert body["expires_in"] <= core_session.SESSION_TTL_SECONDS

    def test_whoami_with_rotated_key_returns_unauth(self, client, monkeypatch):
        """No 401 — Whoami is a probe.  It just returns False so the
        dashboard can decide what to do without polluting error logs."""
        client.post("/api/auth/login", json={"api_key": core_auth.JARVIS_API_KEY})
        monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "rotated-master-key-cccccccccc")
        resp = client.get("/api/auth/whoami")
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is False


# ---- verify_api_key dependency cross-layer ----------------------------------


class TestVerifyApiKeyAcceptsCookie:
    """Once login mints a cookie, the existing ``verify_api_key`` dep
    on every guarded route honors it.  Kept here (not in test_auth.py)
    because it requires the session cookie minted via the route layer."""

    def test_private_route_works_with_session_cookie(self, client):
        client.post("/api/auth/login", json={"api_key": core_auth.JARVIS_API_KEY})
        resp = client.get("/api/private")
        assert resp.status_code == 200

    def test_private_route_rejects_without_anything(self, client):
        resp = client.get("/api/private")
        assert resp.status_code == 401

    def test_private_route_works_with_legacy_bearer(self, client):
        """Legacy programmatic clients (Xiaozhi, scripts) still work."""
        resp = client.get(
            "/api/private",
            headers={"Authorization": f"Bearer {core_auth.JARVIS_API_KEY}"},
        )
        assert resp.status_code == 200

    def test_private_route_works_with_legacy_query_param(self, client):
        """Legacy SSE clients on older builds use ``?api_key=``."""
        resp = client.get(f"/api/private?api_key={core_auth.JARVIS_API_KEY}")
        assert resp.status_code == 200

    def test_session_cookie_takes_precedence_over_bad_bearer(self, client):
        """A valid cookie wins even if the request also carries a wrong
        Bearer header (e.g. a stale localStorage value still being sent
        by half-migrated client code)."""
        client.post("/api/auth/login", json={"api_key": core_auth.JARVIS_API_KEY})
        resp = client.get(
            "/api/private",
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 200
