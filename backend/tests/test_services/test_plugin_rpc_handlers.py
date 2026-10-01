import asyncio
import tempfile
from pathlib import Path

import pytest

from services import plugin_rpc_handlers as handlers
from services.runtime_rpc import RuntimeRpcServer
from tools.runtime_rpc_client import RuntimeRpcClient


@pytest.fixture
def short_socket():
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="plg-rpc-") as directory:
        yield str(Path(directory) / "rpc.sock")


@pytest.mark.asyncio
async def test_rpc_permission_denial_is_forbidden_not_internal_error(
    short_socket, monkeypatch
):
    async def denied(**_):
        raise PermissionError("Only the team orchestrator may manage a subordinate")

    monkeypatch.setattr(handlers, "add", denied)
    server = RuntimeRpcServer(short_socket)
    handlers.register(server)
    await server.start()
    try:
        result = await asyncio.to_thread(
            RuntimeRpcClient(short_socket).call,
            "plugin.add",
            {"caller_agent": "Dev"},
        )
        assert result["status"] == 403
        assert "orchestrator" in result["error"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_rpc_unexpected_error_does_not_expose_credentials(
    short_socket, monkeypatch
):
    async def failed(**_):
        raise RuntimeError("secret credential value")

    monkeypatch.setattr(handlers, "add", failed)
    server = RuntimeRpcServer(short_socket)
    handlers.register(server)
    await server.start()
    try:
        result = await asyncio.to_thread(
            RuntimeRpcClient(short_socket).call,
            "plugin.add",
            {"caller_agent": "Jarvis"},
        )
        assert result["status"] == 500
        assert "secret" not in str(result)
    finally:
        await server.stop()
