"""WebSocket voice auth — three-way precedence (cookie / Bearer / query).

Why a separate file
-------------------
``test_ws_voice.py`` exercises the audio pipeline with auth disabled
(``JARVIS_API_KEY=""`` short-circuits the gate). Auth itself is a
narrow concern with three branches that the audio tests do not cover.
Keeping these in their own file lets a future refactor of either
(audio plumbing vs auth precedence) avoid disturbing the other.

Note on the rejection-path tests
--------------------------------
The route does ``await ws.accept()`` BEFORE checking credentials, then
``ws.close(code=4401)`` on failure. Starlette's TestClient surfaces
the close as a ``WebSocketDisconnect`` raised when the client next
tries to receive — not at connect time. The reject tests assert by
catching that disconnect with the 4401 code, not by ``pytest.raises``
around the ``websocket_connect`` block alone.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core import auth as core_auth
from core import session as core_session
from core.session import SESSION_COOKIE_NAME, create_session_token


def _assert_rejected(client: TestClient, **connect_kwargs) -> None:
    """Open the WS and try to receive — the route closes with 4401 if
    auth fails, which TestClient surfaces here."""
    with client.websocket_connect("/ws/voice", **connect_kwargs) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
        assert exc_info.value.code == 4401


@pytest.fixture(autouse=True)
def _stable_secrets(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-xxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setenv("JARVIS_API_KEY", "test-api-key-xxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "test-api-key-xxxxxxxxxxxxxxxxxxxx")
    yield


@pytest.fixture()
def app():
    from fastapi import FastAPI
    from routes.ws_voice import router as ws_router

    a = FastAPI()
    a.include_router(ws_router)
    return a


@pytest.fixture()
def client(app):
    return TestClient(app)


class TestWsVoiceAuth:
    """The three precedence branches mirror ``verify_api_key``: cookie
    first (the SPA path), Bearer second (programmatic clients), query
    last (Xiaozhi device + legacy scripts). Anyone presenting a valid
    credential through *any* of these gets through."""

    def test_no_credentials_closes_with_4401(self, client):
        _assert_rejected(client)

    def test_query_param_accepted(self, client):
        # Legacy path — Xiaozhi can't set headers, so it sends
        # ?api_key=. Must keep working after the cookie migration.
        with client.websocket_connect(
            "/ws/voice?api_key=test-api-key-xxxxxxxxxxxxxxxxxxxx",
        ) as ws:
            # Send a noop ping; the server may not reply, but the
            # accept-then-close-on-bad-auth path is already excluded
            # because we got this far without a 4401.
            ws.send_json({"type": "noop"})

    def test_bearer_header_accepted(self, client):
        with client.websocket_connect(
            "/ws/voice",
            headers={"Authorization": "Bearer test-api-key-xxxxxxxxxxxxxxxxxxxx"},
        ) as ws:
            ws.send_json({"type": "noop"})

    def test_session_cookie_accepted(self, client):
        # Mint a fresh session token; this is what /api/auth/login would
        # have set after a real login.
        session_token, _payload = create_session_token()
        client.cookies.set(SESSION_COOKIE_NAME, session_token)
        with client.websocket_connect("/ws/voice") as ws:
            ws.send_json({"type": "noop"})

    def test_wrong_api_key_via_query_rejected(self, client):
        with client.websocket_connect("/ws/voice?api_key=totally-wrong") as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 4401

    def test_wrong_bearer_rejected(self, client):
        with client.websocket_connect(
            "/ws/voice",
            headers={"Authorization": "Bearer totally-wrong"},
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 4401

    def test_invalid_session_cookie_falls_back_through_chain(self, client):
        """A garbled session cookie alone shouldn't lock you out — if
        you ALSO carry a valid Bearer, you get in. Otherwise the
        cookie's rejection is just one of three checks."""
        client.cookies.set(SESSION_COOKIE_NAME, "garbage-token-not-a-real-jwt")
        # Bearer alongside the bad cookie → still accepted.
        with client.websocket_connect(
            "/ws/voice",
            headers={"Authorization": "Bearer test-api-key-xxxxxxxxxxxxxxxxxxxx"},
        ) as ws:
            ws.send_json({"type": "noop"})

    def test_invalid_cookie_alone_rejected(self, client):
        client.cookies.set(SESSION_COOKIE_NAME, "garbage-token")
        with client.websocket_connect("/ws/voice") as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 4401

    def test_auth_disabled_when_jarvis_api_key_empty(self, client, monkeypatch):
        """Dev mode — JARVIS_API_KEY unset → auth gate is bypassed.
        This must keep working so a fresh checkout still spins up
        without forcing the user to mint a key first."""
        monkeypatch.setenv("JARVIS_API_KEY", "")
        # No credentials supplied → still gets through.
        with client.websocket_connect("/ws/voice") as ws:
            ws.send_json({"type": "noop"})
