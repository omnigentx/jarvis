"""Independent team-worker stores must not spend a rotating refresh token twice."""

import asyncio

import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import create_engine

from services.plugins.remote_auth import RemoteTokenStore, provider


@pytest.mark.asyncio
async def test_two_workers_refresh_once_and_use_rotated_token(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_MASTER_KEY", "test-only")
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    endpoint = "https://mcp.atlassian.com/v1/mcp/authv2"
    token_url = "https://auth.atlassian.com/oauth/token"
    first = RemoteTokenStore(engine, "candidate", "rovo", endpoint)
    second = RemoteTokenStore(engine, "candidate", "rovo", endpoint)
    first.write("redirect", "http://127.0.0.1:3038/api/plugins/oauth/callback")
    await first.set_client_info(
        OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[first.read()["redirect"]],
            token_endpoint_auth_method="none",
        )
    )
    first.oauth_settings = {
        "token_endpoint": token_url,
        "resource": "https://mcp.atlassian.com/",
    }
    await first.set_tokens(
        OAuthToken(
            access_token="expired", refresh_token="single-use-refresh", expires_in=0
        )
    )
    seen = []
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def respond(request):
        seen.append(request)
        if str(request.url) == token_url:
            refresh_started.set()
            await release_refresh.wait()
            return httpx.Response(
                200,
                json={
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        assert request.headers["Authorization"] == "Bearer rotated-access"
        return httpx.Response(200, json={"ok": True})

    async def call(store):
        async with httpx.AsyncClient(
            auth=provider(store), transport=httpx.MockTransport(respond)
        ) as client:
            return (await client.get(endpoint)).status_code

    one = asyncio.create_task(call(first))
    await asyncio.wait_for(refresh_started.wait(), 2)
    two = asyncio.create_task(call(second))
    # Let the second independent store attempt the locked flow before allowing
    # the first refresh to complete. This is scheduling, not application polling.
    await asyncio.sleep(0)
    release_refresh.set()
    assert await asyncio.wait_for(asyncio.gather(one, two), 3) == [200, 200]
    assert [str(request.url) for request in seen].count(token_url) == 1
    assert (await second.get_tokens()).refresh_token == "rotated-refresh"
    engine.dispose()


@pytest.mark.asyncio
async def test_catalog_rotation_revalidates_new_credential_before_invalidation(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from mcp.types import CallToolResult, TextContent, Tool
    from services.plugins.package import PackageError
    from services.plugins.remote_access import call_account_tool

    monkeypatch.setenv("JARVIS_MASTER_KEY", "test-only")
    store = RemoteTokenStore(
        create_engine(f"sqlite:///{tmp_path / 'catalog.db'}"),
        "candidate",
        "rovo",
        "https://mcp.atlassian.com/v1/mcp/authv2",
    )
    missing = CallToolResult(
        isError=True,
        content=[
            TextContent(
                type="text", text="MCP error -32602: Tool getJiraIssue not found"
            )
        ],
    )
    for recovered in [False, True]:
        await store.set_tokens(OAuthToken(access_token="before-refresh"))
        calls = 0

        async def catalog():
            nonlocal calls
            calls += 1
            if calls == 1:
                # Refresh in this request or consent in another worker changes
                # persisted credentials while the first catalog is in flight.
                await store.set_tokens(OAuthToken(access_token="after-refresh"))
                return SimpleNamespace(tools=[])
            tools = (
                [
                    Tool(
                        name="getAccessibleAtlassianResources",
                        inputSchema={"type": "object"},
                    )
                ]
                if recovered
                else []
            )
            return SimpleNamespace(tools=tools)

        remote = SimpleNamespace(
            call_tool=AsyncMock(return_value=missing),
            list_tools=AsyncMock(side_effect=catalog),
        )
        if recovered:
            assert await call_account_tool(remote, store, "getJiraIssue", {}) == missing
            assert (await store.get_tokens()).access_token == "after-refresh"
        else:
            with pytest.raises(PackageError, match="reconnect"):
                await call_account_tool(remote, store, "getJiraIssue", {})
            assert await store.get_tokens() is None
        assert calls == 2
        remote.call_tool.assert_awaited_once()  # never retry the operation
