"""Spawn Event Socket Server — Unix domain socket IPC for spawn events.

Architecture:
  MCP subprocess (agent_spawner_server.py)
  → connects to Unix domain socket
  → sends SpawnEvents as JSON lines

  Main backend process (this module)
  → asyncio.start_unix_server() listens on socket
  → reads JSON lines from connected clients
  → forwards to SpawnProgressBridge for processing

This replaces the old JSONL file tail-follow approach, providing:
  - Instant event delivery (no 300ms polling)
  - No file position tracking or truncation issues
  - Client disconnect detection
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.spawn_progress_bridge import SpawnProgressBridge

logger = logging.getLogger("spawn_activity")


class _BackendAlreadyRunning(RuntimeError):
    """Raised when start() finds a healthy live socket — another backend
    is already serving on this path. Refuse to clobber it.
    """


class SpawnEventSocketServer:
    """Unix domain socket server for receiving spawn events from MCP subprocesses.

    Usage::

        server = SpawnEventSocketServer(socket_path, bridge)
        await server.start()
        # ... server runs until stop() is called
        await server.stop()
    """

    def __init__(self, socket_path: str, bridge: SpawnProgressBridge) -> None:
        self._socket_path = socket_path
        self._bridge = bridge
        self._server: asyncio.AbstractServer | None = None
        self._client_count = 0
        # Inode of the file we bound at start() — stop() compares before
        # unlinking, so we never wipe another backend's freshly-bound file.
        # (See ROOT_CAUSE: 4 concurrent backends silently wiping each
        # other's sock file, leaving all clients unable to connect.)
        self._bound_inode: int | None = None

    async def start(self) -> None:
        """Create and start the Unix domain socket server.

        Ownership-aware startup:
          1. If the path has no existing file → bind directly.
          2. If a file exists, probe-connect to it.
             - Connect succeeds → another live backend owns it. Raise
               ``_BackendAlreadyRunning`` instead of silently clobbering.
             - Connect fails → file is stale (previous backend exited
               without ``stop()``). Unlink and bind fresh.

        This prevents the previous "everyone unlinks blindly" pattern
        where ``start()`` of backend N would wipe the file that backend
        N-1 was actively listening on, orphaning every subprocess that
        had already connected to N-1.
        """
        socket_file = Path(self._socket_path)
        socket_file.parent.mkdir(parents=True, exist_ok=True)

        if socket_file.exists():
            if _is_socket_alive(self._socket_path):
                raise _BackendAlreadyRunning(
                    f"Another backend is already listening on "
                    f"{self._socket_path}. Refusing to bind. Stop the "
                    f"existing instance first, or check for zombie "
                    f"uvicorn processes (lsof -nP -iTCP:8000 -sTCP:LISTEN)."
                )
            # File exists but no one is listening → stale, safe to remove.
            logger.warning(
                "[SOCKET] Removing stale socket file (no listener): %s",
                self._socket_path,
            )
            try:
                socket_file.unlink()
            except OSError as exc:
                logger.warning("[SOCKET] Failed to unlink stale: %s", exc)

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self._socket_path,
            # runtime_config events can be very large (agent instructions +
            # skills + tool definitions), exceeding the default 64KB limit.
            limit=4 * 1024 * 1024,  # 4MB
        )

        # Record the inode we just bound. ``stop()`` uses this to decide
        # whether to unlink — if a *different* inode is at the path on
        # shutdown, a peer backend has taken over and we must not wipe it.
        try:
            self._bound_inode = os.stat(self._socket_path).st_ino
        except OSError as exc:
            logger.warning(
                "[SOCKET] Could not record inode for %s: %s",
                self._socket_path, exc,
            )
            self._bound_inode = None

        logger.info(
            "[SOCKET] Listening on %s (inode=%s)",
            self._socket_path, self._bound_inode,
        )

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a connected MCP subprocess client."""
        self._client_count += 1
        client_id = self._client_count
        event_count = 0
        logger.info("[SOCKET] Client #%d connected", client_id)

        try:
            while True:
                try:
                    line = await reader.readline()
                except Exception as read_err:
                    logger.error("[SOCKET] Client #%d readline error after %d events: %s", client_id, event_count, read_err, exc_info=True)
                    break

                if not line:
                    logger.info("[SOCKET] Client #%d EOF after %d events", client_id, event_count)
                    break  # Client disconnected (EOF)

                stripped = line.decode("utf-8", errors="replace").strip()
                if not stripped:
                    continue

                event_count += 1

                try:
                    self._bridge.process_event(stripped)
                except Exception as e:
                    logger.warning(
                        "[SOCKET] Client #%d error processing event #%d: %s",
                        client_id, event_count, e, exc_info=True,
                    )
        except asyncio.CancelledError:
            logger.info("[SOCKET] Client #%d cancelled after %d events", client_id, event_count)
            raise
        except Exception as e:
            logger.error("[SOCKET] Client #%d unhandled error after %d events: %s", client_id, event_count, e, exc_info=True)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("[SOCKET] Client #%d disconnected (processed %d events)", client_id, event_count)

    async def stop(self) -> None:
        """Stop the socket server and clean up.

        Inode-guarded unlink: only remove the file if it matches the
        inode recorded at start(). If a peer backend has bound a new
        file at the same path while we were running, the inode will
        differ — leave their file alone.

        Without this guard, the previous "unlink unconditional" pattern
        meant any backend's shutdown could erase a newer backend's live
        socket, breaking every client that had connected to the newer
        instance.
        """
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("[SOCKET] Server stopped")

        if self._bound_inode is None:
            # Never bound, or inode wasn't recorded — leave the path
            # alone to be safe. Better to leak a sock file than to
            # delete someone else's.
            return

        try:
            current_inode = os.stat(self._socket_path).st_ino
        except FileNotFoundError:
            return  # Nothing to clean up.
        except OSError as exc:
            logger.warning("[SOCKET] stat() failed for %s: %s",
                           self._socket_path, exc)
            return

        if current_inode != self._bound_inode:
            logger.warning(
                "[SOCKET] Skip unlink — path %s now owned by a different "
                "backend (inode %s != ours %s).",
                self._socket_path, current_inode, self._bound_inode,
            )
            return

        try:
            Path(self._socket_path).unlink()
            logger.info("[SOCKET] Cleaned up own sock file (inode=%s)",
                        self._bound_inode)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("[SOCKET] unlink failed: %s", exc)


def _is_socket_alive(path: str, timeout: float = 0.5) -> bool:
    """Probe-connect to a Unix socket. Returns True iff some process is
    actively accepting connections on the given path.

    Used by ``start()`` to distinguish a stale socket file (previous
    backend exited without cleanup) from a live one (another backend
    instance is currently running).
    """
    if not Path(path).exists():
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        return True
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        # Includes ENOENT (race after exists() check), ECONNREFUSED
        # variants, and ENOTSOCK if path is a regular file.
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass
