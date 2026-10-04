"""Exercise the real SDK auth flow with a controlled revoked credential."""

import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import create_engine

from services.plugins.package import PackageError
from services.plugins.remote_auth import RemoteTokenStore, provider

URL = "https://mcp.atlassian.com/v1/mcp/authv2"


@pytest.mark.asyncio
async def test_rejected_token_no_longer_looks_connected(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_MASTER_KEY", "test-only")
    store = RemoteTokenStore(
        create_engine(f"sqlite:///{tmp_path / 'auth.db'}"), "candidate", "rovo", URL
    )
    redirect = "http://127.0.0.1:3038/api/plugins/oauth/callback"
    store.write("redirect", redirect)
    await store.set_tokens(
        OAuthToken(access_token="test-only-revoked", token_type="Bearer")
    )
    await store.set_client_info(
        OAuthClientInformationFull(
            client_id="test-only-client", redirect_uris=[redirect]
        )
    )
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if "oauth-protected-resource" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "resource": "https://mcp.atlassian.com",
                    "authorization_servers": ["https://mcp.atlassian.com"],
                },
            )
        if "oauth-authorization-server" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "issuer": "https://mcp.atlassian.com",
                    "authorization_endpoint": "https://mcp.atlassian.com/authorize",
                    "token_endpoint": "https://mcp.atlassian.com/token",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        return httpx.Response(401)

    async with httpx.AsyncClient(
        auth=provider(store), transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(PackageError, match="reconnect"):
            await client.get(URL)
    assert len(requests) == 3  # one failed request and bounded discovery, no retry loop
    assert await store.get_tokens() is None
    assert await store.get_client_info() is not None
