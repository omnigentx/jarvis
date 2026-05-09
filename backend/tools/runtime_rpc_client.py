"""Synchronous Unix-domain-socket RPC client for MCP subprocesses.

Counterpart to ``services.runtime_rpc.RuntimeRpcServer``. Used by tool
subprocesses to call back into the live backend process: subprocess
serialises a JSON-RPC request, the backend handler runs in the live
event loop (with access to ``state.agent_app`` etc.), the result comes
back over the same socket.

Sync (not async) on purpose: each MCP tool invocation makes one RPC call.
A short-lived blocking socket is simpler than threading an asyncio loop
through FastMCP's tool dispatcher, and the latency cost is negligible
(microseconds for a UDS round-trip on the same host).

The default ``call`` returns a structured envelope that mirrors what the
server emits: either ``{"result": ...}`` semantics (the result dict is
returned directly) or an error envelope ``{"error": str, "status": int}``
that callers can forward to their own clients verbatim.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from typing import Any, Optional


class RuntimeRpcError(RuntimeError):
    """Raised when the RPC channel itself fails (socket missing, parse
    error, id mismatch). NOT raised for backend-reported method errors —
    those come back as a structured ``{"error", "status"}`` payload, the
    same shape the calling MCP tool would surface to the agent.
    """


class RuntimeRpcClient:
    def __init__(self, socket_path: Optional[str] = None) -> None:
        self._socket_path = socket_path or os.environ.get("JARVIS_RUNTIME_RPC_SOCKET", "")
        self._next_id = 0
        self._lock = threading.Lock()

    @property
    def socket_path(self) -> str:
        return self._socket_path

    def is_configured(self) -> bool:
        return bool(self._socket_path)

    def call(
        self,
        method: str,
        params: Optional[dict] = None,
        *,
        timeout: Optional[float] = 30.0,
    ) -> dict:
        """Send one request, return the result dict.

        On backend error (handler raised or method unknown), returns a
        structured envelope ``{"error": str, "status": int}`` so the
        calling MCP tool can pass it straight back to the LLM. The MCP
        tool can distinguish a backend error from success by checking
        for the ``error`` key.

        ``timeout=None`` puts the socket into blocking mode (no
        deadline) — used by long-poll handlers like ``approval.wait``
        that legitimately block until a human resolves an approval.
        Caller is then responsible for retrying on transport drops
        (e.g. backend restart).

        Raises ``RuntimeRpcError`` only for transport-level failures
        (socket missing, parse error, id mismatch).
        """
        if not self._socket_path:
            raise RuntimeRpcError(
                "JARVIS_RUNTIME_RPC_SOCKET not set — main backend's "
                "RuntimeRpcServer either isn't running or didn't propagate "
                "its socket path to this subprocess."
            )

        with self._lock:
            self._next_id += 1
            req_id = self._next_id

        request = json.dumps({"id": req_id, "method": method, "params": params or {}}) + "\n"

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self._socket_path)
        except OSError as exc:
            raise RuntimeRpcError(f"Could not connect to {self._socket_path}: {exc}") from exc

        try:
            sock.sendall(request.encode("utf-8"))
            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    raise RuntimeRpcError("RPC server closed connection before responding")
                buf.extend(chunk)
            line, _ = buf.split(b"\n", 1)
        finally:
            sock.close()

        try:
            response = json.loads(line.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeRpcError(f"Invalid JSON from RPC server: {exc}") from exc

        if response.get("id") != req_id:
            raise RuntimeRpcError(
                f"RPC id mismatch: expected {req_id}, got {response.get('id')}"
            )

        if "error" in response and response["error"]:
            err = response["error"]
            return {
                "error": err.get("message", "RPC error"),
                "status": err.get("code", -1),
                "data": err.get("data"),
            }
        return response.get("result", {})


# Module-level client — convenient for MCP tool functions that don't want
# to plumb a client instance through their own state.
_default_client: Optional[RuntimeRpcClient] = None


def default_client() -> RuntimeRpcClient:
    global _default_client
    if _default_client is None:
        _default_client = RuntimeRpcClient()
    return _default_client


def call(method: str, params: Optional[dict] = None, *, timeout: Optional[float] = 30.0) -> dict:
    """Shortcut for ``default_client().call(...)``. ``timeout=None`` for
    long-poll handlers — see :meth:`RuntimeRpcClient.call`.
    """
    return default_client().call(method, params, timeout=timeout)


__all__ = ["RuntimeRpcClient", "RuntimeRpcError", "default_client", "call"]
