"""Real Unix sockets exercise correlated ACK, disconnect and ownership."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from services.plugins.ipc import PluginControlServer, request_update, socket_path
from services.plugins.lifecycle import RuntimeUncertain


@pytest.mark.asyncio
async def test_real_socket_requires_completed_handler_ack(tmp_path):
    path = socket_path(str(tmp_path / "db"), "run")
    handler = AsyncMock(return_value={"ack": True})
    server = PluginControlServer(path, "run", handler)
    await server.start()
    try:
        assert await request_update(
            path,
            run_id="run",
            candidate=str(uuid.uuid4()),
            digest="a" * 64,
            operation="activate",
        )
        assert handler.await_count == 1
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        await server.close()
    assert not path.exists()


@pytest.mark.asyncio
async def test_timeout_disconnect_cancels_queued_operation(tmp_path):
    path = socket_path(str(tmp_path / "db"), "run")
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    server = PluginControlServer(path, "run", handler)
    await server.start()
    try:
        with pytest.raises(RuntimeUncertain):
            await request_update(
                path,
                run_id="run",
                candidate=str(uuid.uuid4()),
                digest="a" * 64,
                operation="activate",
                timeout=0.05,
            )
        await asyncio.wait_for(cancelled.wait(), 2)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_wrong_run_never_reaches_runtime(tmp_path):
    path = socket_path(str(tmp_path / "db"), "run")
    handler = AsyncMock(return_value={"ack": True})
    server = PluginControlServer(path, "run", handler)
    await server.start()
    try:
        with pytest.raises(RuntimeUncertain):
            await request_update(
                path,
                run_id="other",
                candidate=str(uuid.uuid4()),
                digest="a" * 64,
                operation="activate",
            )
        handler.assert_not_called()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_mismatched_ack_is_uncertain_not_safe_failure(tmp_path):
    import json

    path = socket_path(str(tmp_path / "db"), "mismatch")

    async def reply(reader, writer):
        request = json.loads(await reader.readline())
        writer.write(
            json.dumps({**request, "ack": True, "digest": "b" * 64}).encode() + b"\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(reply, path=path)
    try:
        with pytest.raises(RuntimeUncertain):
            await request_update(
                path,
                run_id="mismatch",
                candidate=str(uuid.uuid4()),
                digest="a" * 64,
                operation="activate",
            )
    finally:
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_active_socket_and_symlink_cannot_be_clobbered(tmp_path):
    path = socket_path(str(tmp_path / "db"), "same")
    first = PluginControlServer(path, "same", AsyncMock())
    await first.start()
    try:
        with pytest.raises(RuntimeError, match="already active"):
            await PluginControlServer(path, "same", AsyncMock()).start()
    finally:
        await first.close()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("unchanged")
    path.symlink_to(sentinel)
    try:
        with pytest.raises(ValueError, match="Unsafe"):
            await PluginControlServer(path, "same", AsyncMock()).start()
        assert sentinel.read_text() == "unchanged"
    finally:
        path.unlink()


@pytest.mark.asyncio
async def test_housekeeping_removes_crash_inode_but_not_live_endpoint():
    import os
    import socket
    import time
    from services.plugins.ipc_maintenance import cleanup_sockets
    import tempfile
    from pathlib import Path

    directory = tempfile.TemporaryDirectory(prefix="plg-", dir="/tmp")
    tmp_path = Path(directory.name)
    abandoned = tmp_path / ("a" * 24 + ".sock")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(abandoned))
    sock.close()
    live = tmp_path / ("b" * 24 + ".sock")
    server = PluginControlServer(live, "run", AsyncMock())
    await server.start()
    old = time.time() - 90000
    for path in (abandoned, live):
        os.utime(path, (old, old))
    try:
        assert await cleanup_sockets(tmp_path) == [abandoned.name]
        assert live.exists()
    finally:
        await server.close()
        directory.cleanup()
