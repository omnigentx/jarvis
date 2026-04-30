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
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.spawn_progress_bridge import SpawnProgressBridge

logger = logging.getLogger("spawn_activity")


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

    async def start(self) -> None:
        """Create and start the Unix domain socket server."""
        # Clean up stale socket file from previous run
        socket_file = Path(self._socket_path)
        socket_file.unlink(missing_ok=True)
        socket_file.parent.mkdir(parents=True, exist_ok=True)

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self._socket_path,
            # runtime_config events can be very large (agent instructions +
            # skills + tool definitions), exceeding the default 64KB limit.
            limit=4 * 1024 * 1024,  # 4MB
        )
        logger.info("[SOCKET] Listening on %s", self._socket_path)

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
        """Stop the socket server and clean up."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("[SOCKET] Server stopped")

        Path(self._socket_path).unlink(missing_ok=True)
