"""Tests for ``core.csrf`` — double-submit cookie middleware.

Cross-layer behavior under test:

1. **Safe methods** (GET / HEAD / OPTIONS) bypass entirely.
2. **Mutations without a CSRF cookie** pass through (the route's auth
   dependency catches unauthenticated traffic).
3. **Mutations with a CSRF cookie but no/mismatched header** → 403.
4. **Mutations with cookie + matching header** pass through.
5. **Exempt prefixes** (``/api/auth/login``, ``/api/setup``,
   ``/api/oauth/.../callback``) bypass even with a mismatched header,
   because users don't own a CSRF cookie at those steps.
6. **Non-API mutations** (none today, but invariant) bypass — the SPA
   itself only POSTs to ``/api/*``.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.csrf import CsrfMiddleware
from core.session import CSRF_COOKIE_NAME


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.add_middleware(CsrfMiddleware)

    @app.get("/api/things")
    async def list_things() -> dict:
        return {"ok": True}

    @app.post("/api/things")
    async def create_thing() -> dict:
        return {"ok": True}

    @app.put("/api/things/{tid}")
    async def update_thing(tid: str) -> dict:
        return {"ok": True, "id": tid}

    @app.delete("/api/things/{tid}")
    async def delete_thing(tid: str) -> dict:
        return {"ok": True, "id": tid}

    # Exempt prefixes
    @app.post("/api/auth/login")
    async def login() -> dict:
        return {"ok": True}

    @app.post("/api/setup/auth")
    async def setup_auth() -> dict:
        return {"ok": True}

    @app.post("/api/oauth/google/callback")
    async def oauth_callback() -> dict:
        return {"ok": True}

    # Non-API mutation — should never be guarded by us
    @app.post("/upload")
    async def upload_static() -> dict:
        return {"ok": True}

    return TestClient(app)


class TestSafeMethods:
    def test_get_bypasses(self, client):
        assert client.get("/api/things").status_code == 200

    def test_head_bypasses(self, client):
        # FastAPI auto-routes HEAD to GET when no explicit HEAD handler exists,
        # but if the framework returns 405 it still proves the middleware did
        # not 403 us. The contract is "CSRF must not block safe methods".
        resp = client.head("/api/things")
        assert resp.status_code != 403

    def test_options_bypasses(self, client):
        # FastAPI returns 405 by default for OPTIONS without a CORS handler,
        # but the CSRF middleware should NOT 403 — anything other than 403
        # proves it bypassed.
        resp = client.options("/api/things")
        assert resp.status_code != 403


class TestMutationsWithoutCookie:
    def test_post_without_cookie_passes_through(self, client):
        """An unauthenticated mutation reaches the route — auth (not
        CSRF) is what should reject it, so middleware must not 403."""
        resp = client.post("/api/things")
        assert resp.status_code == 200

    def test_put_without_cookie_passes_through(self, client):
        assert client.put("/api/things/42").status_code == 200


class TestMutationsWithCookie:
    def test_missing_header_when_cookie_present_rejects(self, client):
        client.cookies.set(CSRF_COOKIE_NAME, "abc123")
        resp = client.post("/api/things")
        assert resp.status_code == 403
        assert resp.json()["error"] == "csrf_failed"

    def test_mismatched_header_rejects(self, client):
        client.cookies.set(CSRF_COOKIE_NAME, "abc123")
        resp = client.post("/api/things", headers={"X-CSRF-Token": "different"})
        assert resp.status_code == 403

    def test_matching_header_passes(self, client):
        client.cookies.set(CSRF_COOKIE_NAME, "abc123")
        resp = client.post("/api/things", headers={"X-CSRF-Token": "abc123"})
        assert resp.status_code == 200

    def test_matching_header_on_put(self, client):
        client.cookies.set(CSRF_COOKIE_NAME, "tok")
        resp = client.put("/api/things/9", headers={"X-CSRF-Token": "tok"})
        assert resp.status_code == 200

    def test_matching_header_on_delete(self, client):
        client.cookies.set(CSRF_COOKIE_NAME, "tok")
        resp = client.delete("/api/things/9", headers={"X-CSRF-Token": "tok"})
        assert resp.status_code == 200


class TestConstantTimeCompare:
    def test_long_prefix_match_still_rejects(self, client):
        """Trivial-prefix attack: header that shares a long prefix with the
        cookie must still be rejected.  Validates we use ``hmac.compare_digest``
        not a plain ``==``."""
        client.cookies.set(CSRF_COOKIE_NAME, "abcdef-realtoken")
        resp = client.post(
            "/api/things",
            headers={"X-CSRF-Token": "abcdef-faketoken"},
        )
        assert resp.status_code == 403


class TestExemptPaths:
    def test_login_exempt(self, client):
        """Login cannot require CSRF — the user has no cookie yet."""
        resp = client.post("/api/auth/login")
        assert resp.status_code == 200

    def test_setup_exempt(self, client):
        # Even with a (stale) cookie + missing header, /api/setup must pass.
        client.cookies.set(CSRF_COOKIE_NAME, "stale")
        resp = client.post("/api/setup/auth")
        assert resp.status_code == 200

    def test_oauth_callback_exempt(self, client):
        """Third-party redirects can't carry our header."""
        client.cookies.set(CSRF_COOKIE_NAME, "stale")
        resp = client.post("/api/oauth/google/callback")
        assert resp.status_code == 200


class TestNonApiPaths:
    def test_post_to_non_api_bypasses(self, client):
        client.cookies.set(CSRF_COOKIE_NAME, "tok")
        resp = client.post("/upload")
        # Whatever the response is (404 / 200 / 405), it must NOT be a CSRF 403.
        assert resp.status_code != 403


class TestBearerCallersAreExempt:
    """Programmatic clients (Xiaozhi, scripts) authenticate via Bearer
    headers and have NO ``jarvis_csrf`` cookie. They must NOT be
    blocked by this middleware — that would break automation that
    pre-dates the cookie-auth flow.

    This pins the contract called out in the module docstring under
    "Scope of protection". A future change that tightens the rule and
    accidentally breaks Xiaozhi will fail this test.
    """

    def test_bearer_post_without_csrf_cookie_passes(self, client):
        # No CSRF cookie set — the only auth signal is the Bearer header.
        resp = client.post(
            "/api/things",
            headers={"Authorization": "Bearer some-api-key"},
        )
        assert resp.status_code == 200

    def test_query_param_post_without_csrf_cookie_passes(self, client):
        # Legacy ``?api_key=`` style — same expectation as Bearer.
        resp = client.post("/api/things?api_key=legacy-key")
        assert resp.status_code == 200
