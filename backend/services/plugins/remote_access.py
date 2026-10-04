"""Readiness contract for the reviewed Atlassian Rovo native adapter."""

import asyncio
from services.plugins.package import PackageError
from services.plugins.remote_auth import RemoteTokenStore


async def require_account_tools(
    tools, store: RemoteTokenStore, observed_tokens: str | None
):
    """Reject the anonymous catalog instead of claiming Atlassian is usable.

    Live Rovo returns four generic Teamwork Graph tools with HTTP 200 for an
    invalid bearer token. The authenticated catalog exposes the account resource
    discovery tool. This is an adapter contract, not a generic MCP heuristic.
    Fail closed if that contract changes; never infer authorization from 200.
    """
    if any(tool.name == "getAccessibleAtlassianResources" for tool in tools):
        return
    async with store.serialized():
        # A different worker may have completed consent/rotation while this
        # list request was in flight. Never erase its newer credential.
        store.invalidate_tokens(expected=observed_tokens, compare=True)
    raise PackageError(
        "Remote account did not authorize Atlassian tools; reconnect in Settings"
    )


async def call_account_tool(
    remote, store: RemoteTokenStore, tool: str, arguments: dict
):
    """Recheck access once when Rovo loses a previously advertised tool.

    A revoked credential can return an MCP 'tool not found' error with HTTP
    200. Do not retry the operation, and do not treat ordinary business errors
    or permission errors as account revocation.
    """
    observed = store.read().get("tokens")
    async with asyncio.timeout(60):
        result = await remote.call_tool(tool, arguments)
        missing = f"MCP error -32602: Tool {tool} not found"
        if result.isError and any(
            getattr(block, "text", "") == missing for block in result.content
        ):
            tools = (await remote.list_tools()).tools
            await require_account_tools(tools, store, observed)
        return result
