"""Transactional plugin MCP attachment through public fast-agent APIs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from fast_agent.mcp.mcp_aggregator import MCPAttachOptions

from services.plugins.lifecycle import RuntimeUncertain
from services.plugins.package import PluginPackage
from services.plugins.transport import connection_settings

logger = logging.getLogger(__name__)


def server_names(package: PluginPackage) -> list[str]:
    return server_names_for(package.digest, package.servers)


def server_names_for(digest: str, servers: dict) -> list[str]:
    # fast-agent caps namespaced tool names at 64 characters. A long server
    # prefix can erase the tool suffix or collide after truncation.
    return [
        "plg-" + hashlib.sha256((digest + "\0" + name).encode()).hexdigest()[:20]
        for name in servers
    ]


async def attach_servers(
    agent: Any,
    root: Path,
    package: PluginPackage,
    *,
    image: str,
    credentials: dict[str, str] | None = None,
) -> bool:
    """Validate every config before opening a process; roll back partial adds.

    Call only inside RuntimeCapabilities.update after explicit policy review.
    A previous attachment is never silently reused as fresh acknowledgement.
    """
    names = server_names(package)
    settings = [
        connection_settings(root, name, config, image, credentials)
        for name, config in package.servers.items()
    ]
    live_before = agent.list_attached_mcp_servers()
    if len([name for name in live_before if name.startswith("plg-")]) + len(names) > 8:
        return False
    if any(name in live_before for name in names):
        return False
    attached: list[str] = []
    try:
        async with asyncio.timeout(60):
            for name, config in zip(names, settings, strict=True):
                # Include the attempted name: discovery may fail after opening
                # a transport, and cancellation must release that transport.
                attached.append(name)
                result = await agent.attach_mcp_server(
                    server_name=name,
                    server_config=config,
                    options=MCPAttachOptions(startup_timeout_seconds=20),
                )
                if result.attached is not True or result.already_attached:
                    raise RuntimeError("MCP runtime did not acknowledge attachment")
                total = getattr(result, "tools_total", None)
                added = getattr(result, "tools_added", [])
                if total is not None and total != len(set(added)):
                    raise RuntimeError("MCP tool namespaces collide")
                if total is not None and total > 128:
                    raise RuntimeError("Plugin tool inventory exceeds runtime budget")
                tools = [
                    tool
                    for tool in (await agent.list_tools()).tools
                    if tool.name.startswith("plg-")
                ]
                if len(tools) > 128:
                    raise RuntimeError("Aggregate plugin tool budget exceeded")
                if (
                    len(json.dumps([tool.model_dump() for tool in tools]).encode())
                    > 262144
                ):
                    raise RuntimeError("Plugin tool schemas exceed runtime budget")
            live = agent.list_attached_mcp_servers()
            if not all(name in live for name in names):
                raise RuntimeError("MCP runtime attachment disappeared")
        return True
    except BaseException as exc:

        async def rollback() -> None:
            for name in reversed(attached):
                try:
                    await agent.detach_mcp_server(name)
                except Exception:  # noqa: BLE001 — runtime boundary; do not expose credential-bearing errors
                    logger.warning("[plugins] MCP rollback unconfirmed server=%s", name)

        cleanup = asyncio.create_task(rollback())
        try:
            async with asyncio.timeout(30):
                await asyncio.shield(cleanup)
        except (asyncio.CancelledError, TimeoutError):
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)
            raise RuntimeUncertain("MCP rollback was interrupted") from None
        if any(name in agent.list_attached_mcp_servers() for name in attached):
            raise RuntimeUncertain("MCP rollback was not acknowledged") from None
        if not isinstance(exc, Exception):
            raise
        return False


async def detach_servers(agent: Any, package: PluginPackage) -> bool:
    """Removal is acknowledged only after every namespaced server is absent."""
    async with asyncio.timeout(30):
        for name in reversed(server_names(package)):
            if name in agent.list_attached_mcp_servers():
                await agent.detach_mcp_server(name)
        return not any(
            name in agent.list_attached_mcp_servers() for name in server_names(package)
        )
