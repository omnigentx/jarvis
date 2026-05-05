"""Runtime RPC bridge — main backend ↔ MCP subprocesses.

Generic Unix-domain-socket request/response channel that lets MCP tool
subprocesses (e.g. ``tools/skill_server.py``) call into the live backend
process. Designed as a reusable pattern: any future tool that needs to
mutate runtime state (skills today; dynamic agent/server creation
tomorrow) can register a handler and have the subprocess invoke it.

Wire format: newline-delimited JSON. Each line is a JSON-RPC-2.0 lite
request or response. Inspired by JSON-RPC but trimmed of the parts we
don't need (notifications, batching). One line in, one line out.

  Request:   ``{"id": 7, "method": "skill.create", "params": {...}}``
  OK reply:  ``{"id": 7, "result": {...}}``
  Err reply: ``{"id": 7, "error": {"code": 400, "message": "...", "data": {...}}}``

Auth: file-system permissions on the socket path (it lives under the
backend's runtime dir). Only processes that can read the path can
connect — same trust model as ``spawn_event_socket``.

The handler dispatcher accepts both sync and async functions. Sync
handlers run in the loop's executor (so they don't block the loop if
they do disk IO).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("runtime_rpc")

Handler = Callable[..., Any]
ParseError = -32700
MethodNotFound = -32601
InvalidParams = -32602
InternalError = -32603


class RuntimeRpcServer:
    """Newline-delimited JSON-RPC server over a Unix domain socket.

    Usage::

        server = RuntimeRpcServer(socket_path)
        server.register("skill.create", skill_create_handler)
        await server.start()
        # ... shutdown ...
        await server.stop()
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._handlers: dict[str, Handler] = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._client_count = 0

    # ---- Registration ---------------------------------------------------

    def register(self, method: str, handler: Handler) -> None:
        if method in self._handlers:
            logger.warning("[RPC] Handler %r overrides existing registration", method)
        self._handlers[method] = handler

    def methods(self) -> list[str]:
        return sorted(self._handlers)

    # ---- Lifecycle ------------------------------------------------------

    async def start(self) -> None:
        path = Path(self._socket_path)
        path.unlink(missing_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self._socket_path,
            # Skill content + agent config can grow; match spawn_event_socket cap.
            limit=4 * 1024 * 1024,
        )
        logger.info("[RPC] Listening on %s with %d method(s)", self._socket_path, len(self._handlers))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        Path(self._socket_path).unlink(missing_ok=True)
        logger.info("[RPC] Stopped")

    # ---- Request handling -----------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._client_count += 1
        client_id = self._client_count
        logger.debug("[RPC] Client #%d connected", client_id)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break  # EOF / disconnect
                stripped = line.strip()
                if not stripped:
                    continue
                response = await self._dispatch_line(stripped)
                writer.write(response.encode("utf-8") + b"\n")
                await writer.drain()
        except asyncio.CancelledError:
            raise
        except ConnectionResetError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("[RPC] Client #%d crashed", client_id)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            logger.debug("[RPC] Client #%d disconnected", client_id)

    async def _dispatch_line(self, line: bytes) -> str:
        try:
            req = json.loads(line.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return _err_response(None, ParseError, f"Parse error: {exc}")

        if not isinstance(req, dict):
            return _err_response(None, InvalidParams, "Request must be a JSON object")

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        if not isinstance(method, str):
            return _err_response(req_id, InvalidParams, "Missing or non-string 'method'")
        if not isinstance(params, dict):
            return _err_response(req_id, InvalidParams, "'params' must be an object")

        handler = self._handlers.get(method)
        if handler is None:
            return _err_response(req_id, MethodNotFound, f"Unknown method: {method}")

        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(**params)
            else:
                # Run sync handlers in default executor to avoid blocking the
                # loop when they do filesystem I/O.
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, lambda: handler(**params))
        except TypeError as exc:
            return _err_response(req_id, InvalidParams, f"Bad arguments: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[RPC] Handler %r raised", method)
            return _err_response(
                req_id, InternalError, str(exc),
                data={"type": type(exc).__name__},
            )

        return json.dumps({"id": req_id, "result": result})


def _err_response(
    req_id: Any,
    code: int,
    message: str,
    *,
    data: Optional[dict] = None,
) -> str:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return json.dumps({"id": req_id, "error": err})
