"""Real OCI isolation and MCP handshake; requires an explicitly built image."""

import asyncio
import json
import os

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from services.plugins.sandbox import sandbox_settings


@pytest.fixture
def image():
    value = os.environ.get("JARVIS_TEST_PLUGIN_IMAGE")
    if not value:
        pytest.skip("Build the pinned sandbox image and set JARVIS_TEST_PLUGIN_IMAGE")
    return value


@pytest.mark.asyncio
async def test_real_container_has_no_host_access_network_or_writes(tmp_path, image):
    root = tmp_path / "plugin"
    root.mkdir(mode=0o755)
    sentinel = tmp_path / "host-secret"
    sentinel.write_text("never expose this file")
    script = root / "probe.py"
    script.write_text(
        """import json, os, socket
result = {'uid': os.getuid(), 'host_visible': os.path.exists(HOST_SENTINEL)}
for key, path in [('package_writable', '/plugin/modified'), ('root_writable', '/etc/modified')]:
    try:
        open(path, 'w').write('bad')
        result[key] = True
    except OSError:
        result[key] = False
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
    result['network'] = True
except OSError:
    result['network'] = False
print(json.dumps(result))
""".replace("HOST_SENTINEL", repr(str(sentinel)))
    )
    script.chmod(0o644)
    settings = sandbox_settings(
        root, {"command": "python", "args": ["/plugin/probe.py"]}, image
    )
    process = await asyncio.create_subprocess_exec(
        settings.command,
        *settings.args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode == 0, stderr.decode()
    assert json.loads(stdout) == {
        "uid": 65532,
        "host_visible": False,
        "package_writable": False,
        "root_writable": False,
        "network": False,
    }


@pytest.mark.asyncio
async def test_real_mcp_tool_is_discovered_and_called_in_sandbox(tmp_path, image):
    tmp_path.chmod(0o755)
    script = tmp_path / "server.py"
    script.write_text("""from mcp.server.fastmcp import FastMCP
server = FastMCP('plugin-evidence')
@server.tool()
def echo(value: str) -> str:
    return 'sandbox:' + value
server.run(transport='stdio')
""")
    script.chmod(0o644)
    settings = sandbox_settings(
        tmp_path, {"command": "python", "args": ["/plugin/server.py"]}, image
    )
    parameters = StdioServerParameters(
        command=settings.command, args=settings.args, env=settings.env
    )
    async with asyncio.timeout(20):
        async with stdio_client(parameters) as (read, write):  # noqa: SIM117 — keep transport/session scopes clear
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == ["echo"]
                result = await session.call_tool("echo", {"value": "live"})
                assert not result.isError
                assert result.content[0].text == "sandbox:live"


@pytest.mark.asyncio
async def test_fast_agent_public_attachment_discovery_call_and_removal(tmp_path, image):
    from fast_agent.agents.agent_types import AgentConfig
    from fast_agent.agents.mcp_agent import McpAgent
    from fast_agent.config import MCPSettings, Settings
    from fast_agent.context import Context
    from fast_agent.mcp_server_registry import ServerRegistry

    from services.plugins.mcp_runtime import attach_servers, detach_servers
    from services.plugins.package import PluginPackage

    tmp_path.chmod(0o755)
    (tmp_path / "server.py").write_text("""from mcp.server.fastmcp import FastMCP
server = FastMCP('plugin-evidence')
@server.tool()
def echo(value: str) -> str:
    return 'sandbox:' + value
server.run(transport='stdio')
""")
    settings = Settings(mcp=MCPSettings(servers={}))
    context = Context(
        config=settings, server_registry=ServerRegistry(settings), no_shell=True
    )
    agent = McpAgent(
        AgentConfig(name="PluginEvidence", servers=[], skills=[]), context=context
    )
    descriptor = PluginPackage(
        "evidence",
        None,
        "portable",
        "a" * 64,
        (),
        {"echo": {"command": "python", "args": ["/plugin/server.py"]}},
        (),
        None,
    )
    await agent.initialize()
    try:
        assert await attach_servers(agent, tmp_path, descriptor, image=image)
        tools = await agent.list_tools()
        name = next(tool.name for tool in tools.tools if tool.name.endswith("echo"))
        result = await agent.call_tool(name, {"value": "agent"})
        assert not result.isError
        assert result.content[0].text == "sandbox:agent"
        assert await detach_servers(agent, descriptor)
        assert not agent.list_attached_mcp_servers()
        assert name not in [tool.name for tool in (await agent.list_tools()).tools]
    finally:
        await agent.shutdown()
