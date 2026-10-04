"""Expiry and rotation survive provider/process recreation without SDK internals."""

import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import create_engine

from services.plugins.remote_auth import RemoteTokenStore, provider
from services.plugins.package import PackageError

URL = "https://mcp.atlassian.com/v1/mcp/authv2"
TOKEN = "https://auth.atlassian.com/oauth/token"


@pytest.mark.asyncio
async def test_expired_token_refreshes_before_request_after_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("JARVIS_MASTER_KEY", "test-only")
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    store = RemoteTokenStore(engine, "candidate", "rovo", URL)
    store.write("redirect", "http://127.0.0.1:3038/api/plugins/oauth/callback")
    await store.set_client_info(
        OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[store.read()["redirect"]],
            token_endpoint_auth_method="none",
        )
    )
    store.oauth_settings = {
        "token_endpoint": TOKEN,
        "resource": "https://mcp.atlassian.com",
    }
    await store.set_tokens(
        OAuthToken(
            access_token="old-access",
            token_type="Bearer",
            refresh_token="old-refresh",
            expires_in=0,
        )
    )
    restarted = RemoteTokenStore(engine, "candidate", "rovo", URL)
    requests = []

    class StreamedToken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"access_token":"new-access","refresh_token":"new-refresh","token_type":"Bearer","expires_in":3600}'

    def respond(request):
        requests.append(request)
        if str(request.url) == TOKEN:
            assert b"old-refresh" in request.content
            return httpx.Response(200, stream=StreamedToken())
        assert request.headers["Authorization"] == "Bearer new-access"
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        auth=provider(restarted), transport=httpx.MockTransport(respond)
    ) as client:
        assert (await client.get(URL)).status_code == 200
    assert [str(r.url) for r in requests] == [TOKEN, URL]
    assert (await store.get_tokens()).refresh_token == "new-refresh"
    async with httpx.AsyncClient(
        auth=provider(RemoteTokenStore(engine, "candidate", "rovo", URL)),
        transport=httpx.MockTransport(respond),
    ) as client:
        assert (await client.get(URL)).status_code == 200
    assert len(requests) == 3  # cached valid credential; no second refresh


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 503])
async def test_refresh_denial_vs_transient_failure(tmp_path, monkeypatch, status):
    monkeypatch.setenv("JARVIS_MASTER_KEY", "test-only")
    store = RemoteTokenStore(
        create_engine(f"sqlite:///{tmp_path / 'auth.db'}"), "candidate", "rovo", URL
    )
    store.write("redirect", "http://127.0.0.1:3038/api/plugins/oauth/callback")
    await store.set_client_info(
        OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[store.read()["redirect"]],
            token_endpoint_auth_method="none",
        )
    )
    store.oauth_settings = {"token_endpoint": TOKEN}
    await store.set_tokens(
        OAuthToken(access_token="old", refresh_token="refresh-private", expires_in=0)
    )
    async with httpx.AsyncClient(
        auth=provider(store),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="upstream-private")
        ),
    ) as client:
        with pytest.raises(PackageError) as error:
            await client.get(URL)
    assert "upstream-private" not in str(error.value)
    assert "refresh-private" not in str(error.value)
    assert bool(await store.get_tokens()) == (status == 503)


@pytest.mark.asyncio
async def test_sdk_consent_persists_discovered_refresh_binding(tmp_path, monkeypatch):
    import json
    from urllib.parse import parse_qs, urlsplit

    monkeypatch.setenv("JARVIS_MASTER_KEY", "test-only")
    store = RemoteTokenStore(
        create_engine(f"sqlite:///{tmp_path / 'auth.db'}"), "candidate", "rovo", URL
    )
    store.write("redirect", "http://127.0.0.1:3038/api/plugins/oauth/callback")
    state = None

    async def redirect(url):
        nonlocal state
        state = parse_qs(urlsplit(url).query)["state"][0]

    async def callback():
        return "test-only-code", state

    def respond(request):
        if "oauth-protected-resource" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "resource": "https://mcp.atlassian.com",
                    "authorization_servers": ["https://auth.atlassian.com"],
                },
            )
        if "oauth-authorization-server" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.atlassian.com",
                    "authorization_endpoint": "https://auth.atlassian.com/authorize",
                    "token_endpoint": TOKEN,
                    "registration_endpoint": "https://auth.atlassian.com/register",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if request.url.path == "/register":
            return httpx.Response(
                201, json={**json.loads(request.content), "client_id": "test-client"}
            )
        if str(request.url) == TOKEN:
            return httpx.Response(
                200,
                json={
                    "access_token": "test-access",
                    "refresh_token": "test-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(
            200 if request.headers.get("Authorization") == "Bearer test-access" else 401
        )

    async with httpx.AsyncClient(
        auth=provider(store, redirect_handler=redirect, callback_handler=callback),
        transport=httpx.MockTransport(respond),
    ) as client:
        assert (await client.get(URL)).status_code == 200
    assert store.token_data()["_jarvis_oauth"] == {
        "token_endpoint": TOKEN,
        "resource": "https://mcp.atlassian.com/",
    }
    assert store.token_data()["_jarvis_expires_at"] is not None
