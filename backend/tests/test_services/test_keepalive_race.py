"""Regression test for the keep-alive race-condition fix in
``isolated_runner.py`` (Sasha 1h2m hang, 2026-05-11).

Producer sequence on a turn advance is:

    bus.send(recipient, "meeting_turn")     # ← persistent: writes inbox file
    auto_wake_if_idle(recipient)            # ← transient: socket connect+send

If the recipient subprocess is still inside its initial ``agent.send()``
when the producer fires, its ``AgentChannel`` has not yet bound the
socket. ``send_signal`` finds no sock file, returns False, and the wake
is silently dropped. Inbox still has the message — but the old keep-alive
went straight into ``channel.listen(timeout=None)``, which blocks forever
because the only signal that would have woken it was just lost.

The fix is in ``isolated_runner.py``: drain inbox at the top of every
loop iteration **before** awaiting the channel. State of "is there work?"
lives in the inbox file; the wake signal is only an optimization to
avoid polling. This file pins three contracts that the fix relies on:

1.  Producer sending wake with no listener bound returns False — proves
    we are reproducing the race window, not testing a fixed signal.
2.  After ``start_server`` + drain, the pre-existing inbox message is
    visible without any wake signal arriving.
3.  ``listen(timeout=N)`` returns None on timeout — the safety-net
    branch in keep-alive (re-drain + ERROR log) depends on this.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Make the vendored fast-agent submodule importable when running tests
# directly from ``backend/`` (matches the pattern used by other
# fast-agent-dependent suites that need PYTHONPATH=fast-agent/src).
_FA_SRC = Path(__file__).parent.parent.parent / "fast-agent" / "src"
if _FA_SRC.exists() and str(_FA_SRC) not in sys.path:
    sys.path.insert(0, str(_FA_SRC))

from fast_agent.spawn.agent_channel import AgentChannel  # noqa: E402
from fast_agent.spawn.message_bus import MessageBus  # noqa: E402


AGENT_NAME = "Sasha [Designer]"


@pytest.fixture
def msgs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "messages"
    d.mkdir()
    return d


# ─── Contract 1: wake before listener = dropped signal ────────────────


def test_wake_before_listener_returns_false(msgs_dir: Path, monkeypatch):
    """Setup invariant: send_signal must return False when no listener
    is bound. If this ever flips to True (e.g. someone adds persistent
    wake queue), the rest of these tests no longer reproduce the race
    and the keep-alive timeout safety net can be revisited.
    """
    # Isolate sock dir so other concurrent tests don't see our channel.
    monkeypatch.setenv("TEAM_WORKSPACE", str(msgs_dir.parent))
    monkeypatch.delenv("SPAWN_PROJECT_DIR", raising=False)

    # No channel.start_server() has been called — sock file does not
    # exist. This is the exact state Sasha was in at 16:50:08 (still
    # finishing agent.send, channel not yet bound).
    delivered = AgentChannel.send_signal(AGENT_NAME, "wake")
    assert delivered is False, (
        "send_signal must return False when no listener is bound. "
        "Otherwise this test is not reproducing the race window."
    )


# ─── Contract 2: drain catches messages sent during race window ───────


@pytest.mark.asyncio
async def test_drain_before_listen_catches_pre_race_message(
    msgs_dir: Path, monkeypatch
):
    """The exact race that hung Sasha for 1h2m.

    Sequence:
    1. Producer fires bus.send + auto_wake while consumer's channel is
       not yet bound. Inbox gets the message; wake is lost.
    2. Consumer enters keep-alive: channel.start_server() binds the
       socket — but the wake signal was sent before this and is gone.
    3. Fix: drain inbox BEFORE listen. Message recovered.

    The old code did the opposite (listen first, drain after) — so
    after step 2 the consumer slept forever on listen() while the
    message sat unread in the inbox.
    """
    monkeypatch.setenv("TEAM_WORKSPACE", str(msgs_dir.parent))
    monkeypatch.delenv("SPAWN_PROJECT_DIR", raising=False)

    bus = MessageBus(messages_dir=str(msgs_dir))

    # --- Producer fires while consumer still in agent.send (race) ---
    bus.send(
        from_name="Producer",
        to_name=AGENT_NAME,
        content="🎙️ YOUR TURN TO SPEAK",
        message_type="meeting_turn",
    )
    wake_delivered = AgentChannel.send_signal(AGENT_NAME, "wake")
    assert wake_delivered is False  # signal lost — race reproduced

    # --- Consumer enters keep-alive: start channel, then drain ---
    channel = AgentChannel(AGENT_NAME)
    await channel.start_server()
    try:
        # Old code skipped this step and went straight to listen().
        # Fix calls bus.read_unread() here, before any listen().
        unread = bus.read_unread(AGENT_NAME)

        assert len(unread) == 1, (
            "Drain-before-listen must find the message that was queued "
            "during the race window. Found %d unread." % len(unread)
        )
        assert unread[0].message_type == "meeting_turn"
        assert "YOUR TURN" in unread[0].content
    finally:
        await channel.stop()


# ─── Contract 3: listen timeout returns None for safety-net branch ────


@pytest.mark.asyncio
async def test_listen_timeout_returns_none(msgs_dir: Path, monkeypatch):
    """Keep-alive's 30s timeout fallback relies on listen() returning
    None on TimeoutError. The loop then runs ``last_was_timeout=True``
    so the next drain iteration can log ``wake_signal_missed`` ERROR
    if any unread is found.

    If listen() ever raises instead of returning None (e.g., asyncio
    API change), the safety-net path stops working and the only
    remaining defense is the initial drain at iteration 0.
    """
    monkeypatch.setenv("TEAM_WORKSPACE", str(msgs_dir.parent))
    monkeypatch.delenv("SPAWN_PROJECT_DIR", raising=False)

    channel = AgentChannel(AGENT_NAME)
    await channel.start_server()
    try:
        signal = await channel.listen(timeout=0.1)
        assert signal is None, (
            "listen(timeout=N) must return None on TimeoutError — "
            "keep-alive's wake_signal_missed safety net depends on this."
        )
    finally:
        await channel.stop()


# ─── Contract 4: signal arrives mid-listen, returns the signal ────────


@pytest.mark.asyncio
async def test_signal_during_listen_is_received(msgs_dir: Path, monkeypatch):
    """Once channel.start_server() has bound the sock, an incoming wake
    signal must be delivered to a concurrent listen() — this is the
    fast path the keep-alive loop relies on for low-latency turn
    notifications between team members.
    """
    monkeypatch.setenv("TEAM_WORKSPACE", str(msgs_dir.parent))
    monkeypatch.delenv("SPAWN_PROJECT_DIR", raising=False)

    channel = AgentChannel(AGENT_NAME)
    await channel.start_server()
    try:
        async def _fire_wake() -> None:
            # Small delay so listen() is awaiting before we send.
            await asyncio.sleep(0.05)
            # send_signal is synchronous and blocks on recv() until the
            # server responds. In production producer + consumer are
            # separate processes, so this is fine. In this single-process
            # test we must offload to a thread or the event loop is
            # blocked and the server-side handler never runs to drain
            # the connection (causing both listen() and send_signal to
            # time out).
            delivered = await asyncio.to_thread(
                AgentChannel.send_signal, AGENT_NAME, "wake"
            )
            assert delivered is True

        wake_task = asyncio.create_task(_fire_wake())
        signal = await channel.listen(timeout=2.0)
        await wake_task

        assert signal == "wake", (
            "Wake signal fired while listen() was awaiting should be "
            "delivered (single-event-loop semantics via _wake_event). "
            "Got signal=%r" % signal
        )
    finally:
        await channel.stop()


# ─── Contract 5: mark_done makes re-drain idempotent ──────────────────


def test_mark_done_prevents_re_processing(msgs_dir: Path, monkeypatch):
    """Keep-alive drains inbox on every iteration (top of loop).
    ``mark_done`` after processing must remove the message from
    subsequent read_unread() returns — otherwise the loop would
    re-feed the same message into ``agent.send()`` indefinitely.
    """
    monkeypatch.setenv("TEAM_WORKSPACE", str(msgs_dir.parent))
    monkeypatch.delenv("SPAWN_PROJECT_DIR", raising=False)

    bus = MessageBus(messages_dir=str(msgs_dir))
    bus.send(
        from_name="Producer",
        to_name=AGENT_NAME,
        content="msg1",
        message_type="ping",
    )

    first = bus.read_unread(AGENT_NAME)
    assert len(first) == 1
    bus.mark_done(AGENT_NAME, first[0].message_id)

    second = bus.read_unread(AGENT_NAME)
    assert len(second) == 0, (
        "After mark_done, read_unread must not return the same message. "
        "Otherwise the keep-alive loop would re-feed it on every "
        "iteration."
    )


# ─── Contract 6: is_alive must probe, not stat ────────────────────────


def test_is_alive_false_when_no_socket(msgs_dir: Path, monkeypatch):
    """No sock file at all — agent has never started or has cleanly
    unlinked. ``is_alive`` must return False so callers
    (``_compute_effective_status`` and ``auto_wake_if_idle``) treat the
    agent as dead.
    """
    monkeypatch.setenv("TEAM_WORKSPACE", str(msgs_dir.parent))
    monkeypatch.delenv("SPAWN_PROJECT_DIR", raising=False)
    assert AgentChannel.is_alive(AGENT_NAME) is False


def test_is_alive_false_for_orphan_sock_after_sigkill(
    msgs_dir: Path, monkeypatch
):
    """The exact case that bit us on 2026-05-12: SIGKILL'd
    isolated_runner leaves the sock file behind because no cleanup
    handler ran. File-stat-based ``is_alive`` returned True, so
    ``_compute_effective_status`` falsely reported the dead agent as
    'idle' instead of 'completed', leaving stale entries on the
    dashboard until next refresh.

    Reproducer: bind+close (mimicking a crashed listener that left the
    file). Probe must return False because connect() now hits a path
    with no accepting socket.
    """
    import socket as _socket

    monkeypatch.setenv("TEAM_WORKSPACE", str(msgs_dir.parent))
    monkeypatch.delenv("SPAWN_PROJECT_DIR", raising=False)

    # Build the same path AgentChannel would use, then leave a stale
    # file behind. ``socket.bind`` then ``close`` (without unlink)
    # mirrors the SIGKILL outcome: dirent stays, no listener.
    from fast_agent.spawn.agent_channel import (  # noqa: E402
        _get_sock_dir,
        _sanitize_name,
    )
    sock_path = _get_sock_dir() / f"{_sanitize_name(AGENT_NAME)}.sock"
    if sock_path.exists():
        sock_path.unlink()
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        s.bind(str(sock_path))
    finally:
        s.close()
    # Sanity: file is present on disk (the trap that fooled the old impl).
    assert sock_path.exists()

    try:
        assert AgentChannel.is_alive(AGENT_NAME) is False, (
            "Orphan sock left by SIGKILL must NOT register as alive — "
            "the multi-signal status helper relies on this to mark "
            "killed agents as 'completed' rather than 'idle'."
        )
    finally:
        sock_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_is_alive_true_when_listener_bound(msgs_dir: Path, monkeypatch):
    """Positive contract: a live ``start_server()`` listener must
    register as alive — otherwise the helper would mis-label running
    agents as 'completed' and the auto-wake gate would respawn
    duplicates.
    """
    monkeypatch.setenv("TEAM_WORKSPACE", str(msgs_dir.parent))
    monkeypatch.delenv("SPAWN_PROJECT_DIR", raising=False)

    channel = AgentChannel(AGENT_NAME)
    await channel.start_server()
    try:
        assert AgentChannel.is_alive(AGENT_NAME) is True
    finally:
        await channel.stop()
    # And False again once cleanly stopped.
    assert AgentChannel.is_alive(AGENT_NAME) is False
