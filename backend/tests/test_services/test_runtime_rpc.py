"""Tests for the generic Runtime RPC bridge.

Real Unix socket round-trip — no mocks for the transport. That's the
whole point: this is the layer that connects MCP subprocesses to the
live backend, so a unit test that mocks the socket would test nothing.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services.runtime_rpc import RuntimeRpcServer
from tools.runtime_rpc_client import RuntimeRpcClient, RuntimeRpcError


@pytest.fixture()
def short_sock_path():
    """AF_UNIX paths cap at ~104 chars on macOS. pytest's tmp_path lives
    deep under /private/var/folders/... and overflows. Use /tmp + uuid.
    """
    import uuid
    p = Path("/tmp") / f"rpc-{uuid.uuid4().hex[:10]}.sock"
    yield p
    p.unlink(missing_ok=True)


@pytest.fixture()
async def server(short_sock_path):
    """Spin up the RPC server in the test's loop and tear down cleanly."""
    srv = RuntimeRpcServer(str(short_sock_path))
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


def _client_call(socket_path: str, method: str, params: dict | None = None) -> dict:
    """Run the sync client in a worker thread so the test loop keeps moving
    while the request blocks on socket I/O.
    """
    result_box: dict = {}
    err_box: list = []

    def _go():
        try:
            client = RuntimeRpcClient(socket_path)
            result_box["v"] = client.call(method, params)
        except Exception as exc:
            err_box.append(exc)

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    return t, result_box, err_box


async def _await_thread_result(t, result_box, err_box, *, timeout=5.0):
    """Wait for a worker thread to finish without blocking the event loop."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, t.join, timeout)
    if t.is_alive():
        raise AssertionError("RPC client thread hung")
    if err_box:
        raise err_box[0]
    return result_box.get("v")


# ----- Basic dispatch ----------------------------------------------------


@pytest.mark.asyncio
async def test_sync_handler_round_trip(server):
    server.register("ping", lambda: {"pong": True})
    t, r, e = _client_call(server._socket_path, "ping")
    result = await _await_thread_result(t, r, e)
    assert result == {"pong": True}


@pytest.mark.asyncio
async def test_async_handler_round_trip(server):
    async def echo(*, msg: str) -> dict:
        await asyncio.sleep(0)  # exercise the await path
        return {"echo": msg}

    server.register("echo", echo)
    t, r, e = _client_call(server._socket_path, "echo", {"msg": "hi"})
    result = await _await_thread_result(t, r, e)
    assert result == {"echo": "hi"}


# ----- Error envelope ---------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_method_returns_error_envelope(server):
    t, r, e = _client_call(server._socket_path, "does.not.exist")
    result = await _await_thread_result(t, r, e)
    assert result["status"] == -32601
    assert "Unknown method" in result["error"]


@pytest.mark.asyncio
async def test_handler_exception_returns_error_envelope(server):
    def explode():
        raise ValueError("boom")
    server.register("explode", explode)

    t, r, e = _client_call(server._socket_path, "explode")
    result = await _await_thread_result(t, r, e)
    assert result["status"] == -32603
    assert "boom" in result["error"]
    assert result["data"]["type"] == "ValueError"


@pytest.mark.asyncio
async def test_bad_arguments_returns_invalid_params(server):
    def needs_x(*, x: int) -> dict:
        return {"x": x}

    server.register("needs_x", needs_x)
    t, r, e = _client_call(server._socket_path, "needs_x")  # missing x
    result = await _await_thread_result(t, r, e)
    assert result["status"] == -32602
    assert "Bad arguments" in result["error"]


# ----- Concurrency ------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_clients_get_correct_results(server):
    """Each request gets the response with its own id back, even if many
    requests interleave on the same server.
    """
    async def slow(*, n: int) -> dict:
        await asyncio.sleep(0.05)
        return {"value": n * 2}

    server.register("slow", slow)

    threads = []
    for i in range(5):
        t, r, e = _client_call(server._socket_path, "slow", {"n": i})
        threads.append((i, t, r, e))

    for i, t, r, e in threads:
        result = await _await_thread_result(t, r, e, timeout=10.0)
        assert result == {"value": i * 2}


# ----- Transport errors -------------------------------------------------


def test_client_without_socket_raises_clear_error(monkeypatch):
    # Strip the env var explicitly: the constructor falls back to it when
    # the explicit path is empty, so leaving a stale process env value in
    # place (e.g. when the dev backend is running) would mask the test.
    monkeypatch.delenv("JARVIS_RUNTIME_RPC_SOCKET", raising=False)
    client = RuntimeRpcClient(socket_path="")
    with pytest.raises(RuntimeRpcError) as exc:
        client.call("anything")
    assert "JARVIS_RUNTIME_RPC_SOCKET" in str(exc.value)


def test_client_with_dead_socket_raises_clear_error(tmp_path):
    # Path doesn't exist — connect() should fail.
    client = RuntimeRpcClient(socket_path=str(tmp_path / "nope.sock"))
    with pytest.raises(RuntimeRpcError) as exc:
        client.call("anything")
    assert "Could not connect" in str(exc.value)


# ----- Per-method timeout -----------------------------------------------


@pytest.mark.asyncio
async def test_default_timeout_still_applies(server):
    """Existing handlers (registered without an explicit timeout kwarg)
    keep the default 30s deadline. Covers backwards compatibility for
    skill_rpc_handlers / mcp_rpc_handlers which call ``register(name,
    handler)`` positional.
    """
    from services import runtime_rpc as rrpc

    # Patch the default down so the test runs in milliseconds; restore
    # automatically when the test ends so unrelated tests aren't slowed.
    original = rrpc.DEFAULT_HANDLER_TIMEOUT
    rrpc.DEFAULT_HANDLER_TIMEOUT = 0.1
    try:
        async def slow():
            await asyncio.sleep(1.0)
            return {"unreachable": True}

        server.register("slow_default", slow)  # no timeout kwarg

        t, r, e = _client_call(server._socket_path, "slow_default")
        result = await _await_thread_result(t, r, e, timeout=5.0)
    finally:
        rrpc.DEFAULT_HANDLER_TIMEOUT = original

    assert result["status"] == rrpc.RequestTimeout
    assert "deadline" in result["error"]


@pytest.mark.asyncio
async def test_unbounded_handler_with_timeout_none(server):
    """``timeout=None`` opts out of the dispatch deadline. Used by
    long-poll handlers like ``approval.wait`` that legitimately block
    until something signals them.
    """
    from services import runtime_rpc as rrpc

    # Force the default to a tiny value so the test FAILS unless the
    # opt-out actually works.
    original = rrpc.DEFAULT_HANDLER_TIMEOUT
    rrpc.DEFAULT_HANDLER_TIMEOUT = 0.05
    try:
        async def long_poll():
            # Sleep longer than the default — and longer than the test
            # would tolerate if dispatch enforced the default.
            await asyncio.sleep(0.3)
            return {"signalled": True}

        server.register("long_poll", long_poll, timeout=None)

        t, r, e = _client_call(server._socket_path, "long_poll")
        result = await _await_thread_result(t, r, e, timeout=5.0)
    finally:
        rrpc.DEFAULT_HANDLER_TIMEOUT = original

    assert result == {"signalled": True}


@pytest.mark.asyncio
async def test_per_method_timeout_override(server):
    """An explicit ``timeout=`` on register supersedes the default."""
    async def slow():
        await asyncio.sleep(1.0)

    server.register("slow_custom", slow, timeout=0.05)

    t, r, e = _client_call(server._socket_path, "slow_custom")
    result = await _await_thread_result(t, r, e, timeout=5.0)
    from services import runtime_rpc as rrpc
    assert result["status"] == rrpc.RequestTimeout
