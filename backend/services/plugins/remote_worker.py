"""Host-owned MCP transport bridge; executes no downloaded plugin code.

The official MCP SDK owns OAuth/refresh and the remote protocol. fast-agent
attaches this bridge through its public MCP APIs, including team subprocesses.
"""

from __future__ import annotations

import asyncio
import base64
import sys
import httpx
from mcp import ClientSession
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import create_engine

from services.plugins.lifecycle import PluginLifecycle
from services.plugins.package import inspect_package
from services.plugins.remote_auth import RemoteTokenStore, approved_endpoint, provider
from services.plugins.remote_access import call_account_tool, list_account_tools


async def serve(database: str, runtime_root: str, candidate: str, name: str):
    from pathlib import Path

    engine = create_engine("sqlite:///" + database)
    lifecycle = PluginLifecycle(engine, Path(runtime_root))
    record = lifecycle.get(candidate)
    root = lifecycle.package_path(candidate)
    package = inspect_package(root)
    if record["status"] == "expired" or package.digest != record["digest"]:
        raise RuntimeError("Plugin authorization unavailable")
    endpoint = approved_endpoint(package.servers[name])
    store = RemoteTokenStore(engine, candidate, name, endpoint)
    async with httpx.AsyncClient(
        auth=provider(store), timeout=30, follow_redirects=False
    ) as client:
        async with streamable_http_client(endpoint, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as remote:
                await remote.initialize()
                bridge = Server("jarvis-remote-plugin")

                @bridge.list_tools()
                async def list_tools():
                    return await list_account_tools(remote, store)

                @bridge.call_tool()
                async def call_tool(tool: str, arguments: dict):
                    return await call_account_tool(remote, store, tool, arguments)

                @bridge.list_resources()
                async def list_resources():
                    return (await remote.list_resources()).resources

                @bridge.list_resource_templates()
                async def list_templates():
                    return (await remote.list_resource_templates()).resourceTemplates

                @bridge.read_resource()
                async def read_resource(uri):
                    result = await remote.read_resource(uri)
                    from mcp.server.lowlevel.helper_types import ReadResourceContents

                    return [
                        ReadResourceContents(
                            content=x.text
                            if hasattr(x, "text")
                            else base64.b64decode(x.blob),
                            mime_type=x.mimeType,
                        )
                        for x in result.contents
                    ]

                @bridge.list_prompts()
                async def list_prompts():
                    return (await remote.list_prompts()).prompts

                @bridge.get_prompt()
                async def get_prompt(prompt: str, arguments: dict | None):
                    return await remote.get_prompt(prompt, arguments)

                async with stdio_server() as (read, write):
                    await bridge.run(
                        read, write, bridge.create_initialization_options()
                    )
    engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(serve(*sys.argv[1:]))
    except Exception:
        # Upstream exceptions may contain OAuth tokens or a callback URL.
        print(
            "Remote plugin connection failed; reconnect the account in Settings.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
