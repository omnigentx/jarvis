"""Regression test for spawn-event socket ownership cleanup.

Background: ``services/spawn_event_socket.py`` previously used
unconditional ``unlink(missing_ok=True)`` in both ``start()`` and
``stop()``. When two backend instances ran concurrently (user typed
``uv run uvicorn`` twice without killing the first), this caused:

* **start() clobber** — backend N's ``start()`` would unlink the file
  bound by backend N-1, orphaning every subprocess client connected
  to N-1 (their FD still pointed to N-1's inode, but the path → inode
  mapping in the filesystem now pointed elsewhere or nowhere).
* **stop() clobber** — backend N-1's ``stop()`` would unlink the path
  even though backend N had already taken it over, leaving N with an
  orphan FD and no path that new clients could connect to.

Verified state at fix time (2026-05-12): 4 concurrent uvicorn
processes, ``lsof`` showed 4 different inodes bound to the same path,
``ls`` showed the path did not exist — every subprocess emit_event
was a silent drop, causing the 1h2m Sasha hang root-cause chain.

This file pins the fix:

1. ``start()`` refuses to bind if a live listener already owns the path.
2. ``start()`` removes a stale file (no listener) and binds fresh.
3. ``stop()`` only unlinks if the inode at the path still matches what
   we bound. If a peer has taken over, leave it alone.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure vendored fast-agent submodule is importable (matches the path
# trick used by ``test_keepalive_race.py``).
_FA_SRC = Path(__file__).parent.parent.parent / "fast-agent" / "src"
if _FA_SRC.exists() and str(_FA_SRC) not in sys.path:
    sys.path.insert(0, str(_FA_SRC))

from services.spawn_event_socket import (  # noqa: E402
    SpawnEventSocketServer,
    _BackendAlreadyRunning,
    _is_socket_alive,
)


@pytest.fixture
def short_sock(tmp_path: Path) -> str:
    """Return a path safely under the macOS 104-byte sun_path limit.

    The repo path is long; we use /tmp directly so the test can bind
    AF_UNIX sockets without ENAMETOOLONG.
    """
    d = Path(tempfile.mkdtemp(prefix="ses_", dir="/tmp"))
    return str(d / "spawn_events.sock")


# ─── Contract 1: liveness probe ──────────────────────────────────────


def test_is_socket_alive_returns_false_for_missing_file(short_sock):
    assert _is_socket_alive(short_sock) is False


def test_is_socket_alive_returns_false_for_stale_file(short_sock):
    """A leftover file with no listener should be detected as stale.
    ``start()`` relies on this to safely unlink stale files.
    """
    Path(short_sock).touch()
    assert _is_socket_alive(short_sock) is False
    Path(short_sock).unlink()


@pytest.mark.asyncio
async def test_is_socket_alive_returns_true_for_live_listener(short_sock):
    """A real listener should be detected as alive — used by start()
    to refuse to clobber a peer backend that's still serving clients.
    """
    server = SpawnEventSocketServer(short_sock, bridge=MagicMock())
    await server.start()
    try:
        assert _is_socket_alive(short_sock) is True
    finally:
        await server.stop()


# ─── Contract 2: start() — clobber prevention ────────────────────────


@pytest.mark.asyncio
async def test_start_refuses_to_clobber_live_peer(short_sock):
    """If a peer is alive on the path, start() must NOT silently
    overwrite. Raise ``_BackendAlreadyRunning`` so the user/operator
    can fix the multi-instance situation explicitly.
    """
    peer = SpawnEventSocketServer(short_sock, bridge=MagicMock())
    await peer.start()
    try:
        intruder = SpawnEventSocketServer(short_sock, bridge=MagicMock())
        with pytest.raises(_BackendAlreadyRunning):
            await intruder.start()
        # Peer's file must still be intact (not unlinked by intruder).
        assert Path(short_sock).exists()
        assert _is_socket_alive(short_sock) is True
    finally:
        await peer.stop()


@pytest.mark.asyncio
async def test_start_recovers_from_stale_file(short_sock):
    """If a previous backend exited without cleanup, start() removes
    the stale file and binds fresh. This is the "happy reboot" path.
    """
    Path(short_sock).touch()  # Stale file from dead backend.
    server = SpawnEventSocketServer(short_sock, bridge=MagicMock())
    await server.start()
    try:
        assert _is_socket_alive(short_sock) is True
    finally:
        await server.stop()


# ─── Contract 3: stop() — inode-guarded unlink ───────────────────────


@pytest.mark.asyncio
async def test_stop_unlinks_own_file(short_sock):
    """Happy path: server binds, stops, file is gone."""
    server = SpawnEventSocketServer(short_sock, bridge=MagicMock())
    await server.start()
    bound_inode = server._bound_inode
    assert bound_inode is not None

    await server.stop()
    assert not Path(short_sock).exists()


@pytest.mark.asyncio
async def test_stop_skips_unlink_when_inode_does_not_match(short_sock):
    """The inode-guard branch of stop(): when the file at the path no
    longer matches what we bound (peer took over after we crashed /
    were forced shut), don't unlink.

    asyncio's ``Server.close()`` already happens to be inode-aware
    (verified empirically: it skips unlink if the file at the path
    has a different inode than the one it bound). Our explicit guard
    is defense-in-depth: it documents the intent and guards the case
    where asyncio internals might not catch it (e.g., partial-shutdown
    paths, future asyncio behavior changes).

    Approach: bind A, then fake ``_bound_inode`` to a sentinel that
    can never match a real file. Drop a fresh sentinel file at the
    path. ``stop()`` must see the mismatch and refuse to unlink the
    sentinel file.
    """
    server_a = SpawnEventSocketServer(short_sock, bridge=MagicMock())
    await server_a.start()

    # Replace asyncio's bound file with a sentinel regular file so we
    # can observe whether stop() unlinks "the wrong file".
    # First, manually close the asyncio server to release the bind
    # (asyncio will unlink its own file as part of close()).
    server_a._server.close()
    await server_a._server.wait_closed()
    server_a._server = None  # Prevent stop() from re-closing.

    # asyncio cleaned up the socket file. Place a sentinel file with a
    # different inode — this represents a peer backend's freshly-bound
    # file at the same path.
    sentinel = Path(short_sock)
    sentinel.touch()
    sentinel_inode = sentinel.stat().st_ino
    assert sentinel_inode != server_a._bound_inode, (
        "Setup: sentinel must have a different inode than what A bound."
    )

    # stop() now: should see inode mismatch and skip the unlink.
    await server_a.stop()
    assert sentinel.exists(), (
        "Inode-guarded stop() must NOT unlink a file with a different "
        "inode (would corrupt a peer backend's freshly-bound socket)."
    )

    # Cleanup the sentinel.
    sentinel.unlink()


@pytest.mark.asyncio
async def test_stop_is_idempotent(short_sock):
    """Calling stop() twice must not raise — common during shutdown
    races where multiple cleanup paths converge.
    """
    server = SpawnEventSocketServer(short_sock, bridge=MagicMock())
    await server.start()
    await server.stop()
    await server.stop()  # Should be a no-op, not raise.
