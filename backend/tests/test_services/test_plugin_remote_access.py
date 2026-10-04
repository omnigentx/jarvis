"""Rovo can return HTTP 200 with an anonymous, non-Atlassian tool catalog."""

import pytest
from mcp.shared.auth import OAuthToken
from mcp.types import Tool
from sqlalchemy import create_engine

from services.plugins.package import PackageError
from services.plugins.remote_auth import RemoteTokenStore


@pytest.mark.asyncio
async def test_anonymous_catalog_invalidates_only_observed_credential(
    tmp_path, monkeypatch
):
    from services.plugins.remote_access import require_account_tools

    monkeypatch.setenv("JARVIS_MASTER_KEY", "test-only")
    store = RemoteTokenStore(
        create_engine(f"sqlite:///{tmp_path / 'auth.db'}"),
        "candidate",
        "rovo",
        "https://mcp.atlassian.com/v1/mcp/authv2",
    )
    await store.set_tokens(OAuthToken(access_token="old-invalid", token_type="Bearer"))
    observed = store.read()["tokens"]
    anonymous = [Tool(name="getTeamworkGraphContext", inputSchema={"type": "object"})]
    with pytest.raises(PackageError, match="reconnect"):
        await require_account_tools(anonymous, store, observed)
    assert await store.get_tokens() is None

    await store.set_tokens(OAuthToken(access_token="old-invalid", token_type="Bearer"))
    observed = store.read()["tokens"]
    await store.set_tokens(OAuthToken(access_token="new-valid", token_type="Bearer"))
    with pytest.raises(PackageError, match="reconnect"):
        await require_account_tools(anonymous, store, observed)
    assert (await store.get_tokens()).access_token == "new-valid"
    authenticated = [
        Tool(name="getAccessibleAtlassianResources", inputSchema={"type": "object"})
    ]
    await require_account_tools(authenticated, store, store.read()["tokens"])


@pytest.mark.asyncio
async def test_runtime_missing_tool_rechecks_once_without_retrying_write(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from mcp.types import CallToolResult, TextContent
    from services.plugins.remote_access import call_account_tool

    monkeypatch.setenv("JARVIS_MASTER_KEY", "test-only")
    store = RemoteTokenStore(
        create_engine(f"sqlite:///{tmp_path / 'auth.db'}"),
        "candidate",
        "rovo",
        "https://mcp.atlassian.com/v1/mcp/authv2",
    )
    await store.set_tokens(OAuthToken(access_token="old-invalid", token_type="Bearer"))
    error = CallToolResult(
        isError=True,
        content=[
            TextContent(
                type="text", text="MCP error -32602: Tool getJiraIssue not found"
            )
        ],
    )
    remote = SimpleNamespace(
        call_tool=AsyncMock(return_value=error),
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])),
    )
    with pytest.raises(PackageError, match="reconnect"):
        await call_account_tool(remote, store, "getJiraIssue", {})
    remote.call_tool.assert_awaited_once()
    remote.list_tools.assert_awaited_once()
    assert await store.get_tokens() is None

    await store.set_tokens(OAuthToken(access_token="valid", token_type="Bearer"))
    business_error = CallToolResult(
        isError=True,
        content=[TextContent(type="text", text="403: issue access denied")],
    )
    remote.call_tool = AsyncMock(return_value=business_error)
    remote.list_tools.reset_mock()
    assert await call_account_tool(remote, store, "getJiraIssue", {}) == business_error
    remote.list_tools.assert_not_awaited()
    assert (await store.get_tokens()).access_token == "valid"
