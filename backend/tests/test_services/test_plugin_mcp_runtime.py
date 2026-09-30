"""A partial or unreviewed MCP installation cannot claim Ready."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.plugins.mcp_runtime import attach_servers, server_names
from services.plugins.package import PackageError, PluginPackage

IMAGE = "sha256:" + "a" * 64


def package(servers):
    return PluginPackage("test", None, "portable", "b" * 64, (), servers, (), None)


@pytest.mark.asyncio
async def test_validation_finishes_before_first_process_starts(tmp_path):
    descriptor = package(
        {"first": {"command": "python"}, "evil": {"command": "docker"}}
    )
    agent = SimpleNamespace(
        attach_mcp_server=AsyncMock(), list_attached_mcp_servers=list
    )
    with pytest.raises(PackageError):
        await attach_servers(agent, tmp_path, descriptor, image=IMAGE)
    agent.attach_mcp_server.assert_not_called()


@pytest.mark.asyncio
async def test_partial_attachment_is_removed_in_reverse_order(tmp_path):
    descriptor = package(
        {"first": {"command": "python"}, "second": {"command": "python"}}
    )
    agent = SimpleNamespace(
        list_attached_mcp_servers=list,
        attach_mcp_server=AsyncMock(
            side_effect=[
                SimpleNamespace(attached=True, already_attached=False),
                RuntimeError("private details"),
            ]
        ),
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])),
        detach_mcp_server=AsyncMock(),
    )
    assert await attach_servers(agent, tmp_path, descriptor, image=IMAGE) is False
    assert [call.args[0] for call in agent.detach_mcp_server.await_args_list] == list(
        reversed(server_names(descriptor))
    )


@pytest.mark.asyncio
async def test_existing_attachment_is_not_a_new_acknowledgement(tmp_path):
    descriptor = package({"first": {"command": "python"}})
    agent = SimpleNamespace(
        list_attached_mcp_servers=lambda: server_names(descriptor),
        attach_mcp_server=AsyncMock(),
    )
    assert await attach_servers(agent, tmp_path, descriptor, image=IMAGE) is False
    agent.attach_mcp_server.assert_not_called()


def test_long_plugin_and_server_names_leave_space_for_tool_namespace():
    descriptor = PluginPackage(
        "a" * 64,
        None,
        "portable",
        "b" * 64,
        (),
        {"c" * 64: {"command": "python"}},
        (),
        None,
    )
    names = server_names(descriptor)
    assert len(names[0]) <= 24
    from services.plugins.mcp_runtime import server_names_for

    assert names[0] != server_names_for("c" * 64, descriptor.servers)[0]


@pytest.mark.asyncio
async def test_failed_rollback_is_uncertain_and_cannot_be_cleaned(tmp_path):
    from services.plugins.lifecycle import RuntimeUncertain

    descriptor = package({"echo": {"command": "python"}})
    agent = SimpleNamespace(
        list_attached_mcp_servers=lambda: server_names(descriptor),
        attach_mcp_server=AsyncMock(side_effect=RuntimeError("initialize failed")),
        detach_mcp_server=AsyncMock(side_effect=RuntimeError("detach failed")),
    )
    # No preexisting attachment, then failed startup leaves one transport.
    agent.list_attached_mcp_servers = __import__(
        "unittest.mock", fromlist=["Mock"]
    ).Mock(side_effect=[[], server_names(descriptor)])
    with pytest.raises(RuntimeUncertain):
        await attach_servers(agent, tmp_path, descriptor, image=IMAGE)


@pytest.mark.asyncio
async def test_namespace_collision_rejects_attachment(tmp_path):
    descriptor = package({"echo": {"command": "python"}})
    agent = SimpleNamespace(
        list_attached_mcp_servers=list,
        attach_mcp_server=AsyncMock(
            return_value=SimpleNamespace(
                attached=True,
                already_attached=False,
                tools_total=2,
                tools_added=["same"],
            )
        ),
        detach_mcp_server=AsyncMock(),
    )
    assert await attach_servers(agent, tmp_path, descriptor, image=IMAGE) is False
    agent.detach_mcp_server.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_aggregate_plugin_server_budget_precedes_process_start(tmp_path):
    descriptor = package({"first": {"command": "python"}})
    agent = SimpleNamespace(
        list_attached_mcp_servers=lambda: ["plg-" + str(i) for i in range(8)],
        attach_mcp_server=AsyncMock(),
    )
    assert await attach_servers(agent, tmp_path, descriptor, image=IMAGE) is False
    agent.attach_mcp_server.assert_not_called()
