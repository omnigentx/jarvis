"""Bounded request/ack transport to a running team agent; no polling."""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

MAX_MESSAGE = 16 * 1024
Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def socket_path(database: str, run_id: str) -> Path:
    digest = hashlib.sha256(str(Path(database).resolve()).encode()).hexdigest()[:16]
    # AF_UNIX on macOS has a 104-byte limit; the default temp path is
    # already long enough to exceed it before adding a run identifier.
    root = Path("/tmp") / f"jarvis-plugin-{os.getuid()}-{digest}"
    if root.is_symlink():
        raise ValueError("Unsafe plugin IPC directory")
    root.mkdir(mode=0o700, exist_ok=True)
    if root.stat().st_uid != os.getuid():
        raise ValueError("Plugin IPC directory belongs to another identity")
    root.chmod(0o700)
    return root / (hashlib.sha256(run_id.encode()).hexdigest()[:24] + ".sock")


class PluginControlServer:
    """Disconnect cancels queued work; no successful ACK without actual apply."""

    def __init__(self, path: Path, run_id: str, handler: Handler) -> None:
        self.path, self.run_id, self.handler = path, run_id, handler
        self.server: asyncio.Server | None = None
        self.inode: int | None = None
        self.connections: set[asyncio.Task] = set()

    async def start(self) -> None:
        if self.path.exists() or self.path.is_symlink():
            if self.path.is_symlink() or not stat.S_ISSOCK(self.path.lstat().st_mode):
                raise ValueError("Unsafe existing plugin socket")
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(self.path), 1
                )
            except (ConnectionRefusedError, FileNotFoundError):
                self.path.unlink(missing_ok=True)
            else:
                writer.close()
                await writer.wait_closed()
                raise RuntimeError("Plugin runtime socket already active")
        self.server = await asyncio.start_unix_server(
            self._receive, path=self.path, limit=MAX_MESSAGE
        )
        self.path.chmod(0o600)
        self.inode = self.path.stat().st_ino
        atexit.register(self._unlink_owned_socket)

    async def _receive(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        owner = asyncio.current_task()
        if owner:
            self.connections.add(owner)
        apply: asyncio.Task | None = None
        disconnected: asyncio.Task | None = None
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            if len(line) > MAX_MESSAGE:
                raise ValueError("Message too large")
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {
                "run_id",
                "request_id",
                "candidate",
                "digest",
                "operation",
            }:
                raise ValueError("Invalid control envelope")
            if request["run_id"] != self.run_id or request["operation"] not in {
                "activate",
                "deactivate",
                "status",
            }:
                raise ValueError("Wrong runtime or operation")
            if any(
                not isinstance(value, str) or len(value) > 128
                for value in request.values()
            ):
                raise ValueError("Invalid control values")
            uuid.UUID(request["request_id"])
            uuid.UUID(request["candidate"])
            if len(request["digest"]) != 64 or any(
                char not in "0123456789abcdef" for char in request["digest"]
            ):
                raise ValueError("Invalid package digest")
            apply = asyncio.create_task(self.handler(request))
            disconnected = asyncio.create_task(reader.read(1))
            done, _ = await asyncio.wait(
                {apply, disconnected}, return_when=asyncio.FIRST_COMPLETED
            )
            if disconnected in done:
                apply.cancel()
                await asyncio.gather(apply, return_exceptions=True)
                return
            result = await apply
            payload = {
                **result,
                "run_id": self.run_id,
                "request_id": request["request_id"],
                "digest": request["digest"],
            }
            encoded = json.dumps(payload).encode() + b"\n"
            if len(encoded) > MAX_MESSAGE:
                raise ValueError("Acknowledgement too large")
            writer.write(encoded)
            await writer.drain()
        except (Exception, asyncio.CancelledError):  # noqa: BLE001, S110 — close protocol boundary without leaking secrets
            # Never serialize exception text: it can contain credentials.
            pass
        finally:
            for task in (apply, disconnected):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (apply, disconnected) if task),
                return_exceptions=True,
            )
            writer.close()
            await writer.wait_closed()
            if owner:
                self.connections.discard(owner)

    def _unlink_owned_socket(self) -> None:
        try:
            if (
                not self.path.is_symlink()
                and self.path.exists()
                and self.path.stat().st_ino == self.inode
            ):
                self.path.unlink()
        except OSError:
            pass

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.connections):
            task.cancel()
        await asyncio.gather(*self.connections, return_exceptions=True)
        self._unlink_owned_socket()
        atexit.unregister(self._unlink_owned_socket)


async def request_update(
    path: Path,
    *,
    run_id: str,
    candidate: str,
    digest: str,
    operation: str,
    timeout: float = 60,
) -> bool:
    request = {
        "run_id": run_id,
        "request_id": str(uuid.uuid4()),
        "candidate": candidate,
        "digest": digest,
        "operation": operation,
    }
    writer = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(path, limit=MAX_MESSAGE)
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            reply = json.loads(await reader.readline())
            correlated = (
                isinstance(reply, dict)
                and isinstance(reply.get("ack"), bool)
                and all(
                    reply.get(key) == request[key]
                    for key in ("run_id", "request_id", "digest")
                )
            )
            if not correlated:
                raise ValueError("Uncorrelated runtime acknowledgement")
            return reply["ack"] is True
    except Exception:
        if writer is not None and operation != "status":
            from services.plugins.lifecycle import RuntimeUncertain

            raise RuntimeUncertain("Plugin runtime acknowledgement was lost") from None
        raise
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
