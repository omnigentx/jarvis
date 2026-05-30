"""Unit tests for WSStreamingSTT lifecycle: state machine, hook replay,
pause/resume gating, ``is_alive`` semantics.

The full async reconnect loop (``_run_ws``) is exercised by a
mock-websockets path so we don't depend on a live cloud STT server in CI.
Real upstream reconnect behaviour is verified manually + by the 2026-05-29
session log analysis (Soniox 408 + auto-recover).
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

from services.stt_backends._ws_streaming import (
    BACKOFF_INITIAL,
    ERROR_CONSECUTIVE_FAILURES,
    WSStreamingSTT,
)
from services.stt_backends.types import STTConnectionState


class _ProbeBackend(WSStreamingSTT):
    """Minimal concrete backend used by every test."""
    WS_URL = "wss://probe.test/__unused__"
    LOG_TAG = "Probe STT"

    def __init__(self):
        super().__init__(sample_rate=16000)
        self.config_messages_sent: list[dict] = []
        self.events_received: list[dict] = []

    def _build_config_message(self):
        msg = {"probe": True, "sr": self._sample_rate}
        self.config_messages_sent.append(msg)
        return msg

    def _handle_event(self, data):
        self.events_received.append(data)


# ═══════════════════════════════════════════════
# 1. State machine (synchronous, no thread)
# ═══════════════════════════════════════════════


class TestStateMachineSync:
    """Test the parts callable WITHOUT the WS thread running."""

    def test_initial_state_is_idle(self):
        svc = _ProbeBackend()
        assert svc.connection_state == STTConnectionState.IDLE

    def test_initial_is_alive_false_no_thread(self):
        """is_alive is False before start_listen_loop because the
        worker thread hasn't been booted yet."""
        svc = _ProbeBackend()
        assert svc.is_alive is False

    def test_set_hook_replays_current_state(self):
        """set_hook MUST emit one ws_status event so a late subscriber
        (e.g. frontend reload mid-session) sees the right chip state."""
        svc = _ProbeBackend()
        events: list[tuple[str, dict]] = []
        svc.set_hook(lambda n, p: events.append((n, p)))
        assert len(events) == 1
        assert events[0][0] == "ws_status"
        assert events[0][1]["state"] == "idle"

    def test_set_hook_none_does_not_emit(self):
        svc = _ProbeBackend()
        # Should not raise; should not enqueue anything (no hook to call).
        svc.set_hook(None)

    def test_set_state_emits_ws_status(self):
        svc = _ProbeBackend()
        events: list[tuple[str, dict]] = []
        svc.set_hook(lambda n, p: events.append((n, p)))
        events.clear()  # discard replay
        svc._set_state(STTConnectionState.CONNECTING, detail="opening")
        assert events == [
            ("ws_status", {"state": "connecting", "attempt": 0, "detail": "opening"})
        ]
        assert svc.connection_state == STTConnectionState.CONNECTING

    def test_set_state_idempotent_does_not_re_emit(self):
        """Calling _set_state with the SAME state + no detail must not
        flood the wire — the chip already knows."""
        svc = _ProbeBackend()
        events: list[tuple[str, dict]] = []
        svc.set_hook(lambda n, p: events.append((n, p)))
        events.clear()
        svc._set_state(STTConnectionState.CONNECTED)
        svc._set_state(STTConnectionState.CONNECTED)
        # Only the first transition emits (the second is a no-op)
        assert len(events) == 1

    def test_emit_partial_skips_empty(self):
        svc = _ProbeBackend()
        events: list[tuple[str, dict]] = []
        svc.set_hook(lambda n, p: events.append((n, p)))
        events.clear()
        svc._emit_partial("")
        svc._emit_partial("   ")
        svc._emit_partial("hello")
        assert len(events) == 1
        assert events[0] == ("partial_transcript", {"text": "hello"})

    def test_emit_endpoint_emits_vad_stop_and_recording_stop_only(self):
        """The 2026-05-29 race fix removed the trailing recording_start
        from _emit_endpoint. This test pins that fix so a refactor can't
        silently re-introduce the duplicate that races with final_transcript
        dispatch.
        """
        svc = _ProbeBackend()
        events: list[tuple[str, dict]] = []
        svc.set_hook(lambda n, p: events.append((n, p)))
        events.clear()
        svc._emit_endpoint()
        names = [e[0] for e in events]
        assert names == ["vad_stop", "recording_stop"], (
            f"_emit_endpoint must emit only vad_stop+recording_stop. "
            f"Got: {names}. If recording_start is back, the barge-in "
            f"auto-submit race from 2026-05-29 will recur."
        )

    def test_shutdown_marks_closed_and_not_alive(self):
        svc = _ProbeBackend()
        svc.shutdown()
        assert svc.is_alive is False


# ═══════════════════════════════════════════════
# 2. Lifecycle with mocked websockets.connect
# ═══════════════════════════════════════════════


class _FakeWS:
    """Async-context-manager compatible fake of a websockets ClientProtocol.

    Supports:
        await ws.send(payload)        # records frames
        async for msg in ws:           # yields scripted server messages then closes
        await ws.close()
    """
    def __init__(self, server_msgs: list[Any] | None = None):
        self._server_msgs = list(server_msgs or [])
        self.sent_frames: list[Any] = []
        self._closed = asyncio.Event()

    async def send(self, payload):
        if self._closed.is_set():
            raise ConnectionError("closed")
        self.sent_frames.append(payload)

    async def close(self):
        self._closed.set()

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        # Yield each scripted server message in turn; on exhaustion, wait
        # until close() is called so the receiver doesn't spin.
        for msg in self._server_msgs:
            yield msg
        await self._closed.wait()


def _patch_websockets_connect(monkeypatch, ws_factory):
    """Patch websockets.connect to return ws_factory()-built fakes.

    ws_factory is a callable returning a (_FakeWS, optional close-delay)
    pair or just a _FakeWS.
    """
    @asynccontextmanager
    async def _fake_connect(*args, **kwargs):
        ws = ws_factory()
        try:
            yield ws
        finally:
            await ws.close()

    import websockets
    monkeypatch.setattr(websockets, "connect", _fake_connect)


@pytest.mark.asyncio
async def test_resume_drives_idle_to_connected(monkeypatch):
    """resume() → state transitions IDLE → CONNECTING → CONNECTED, and
    the config message is sent to the upstream WS."""

    svc = _ProbeBackend()
    events: list[tuple[str, dict]] = []
    svc.set_hook(lambda n, p: events.append((n, p)))

    fake_ws = _FakeWS(server_msgs=[])  # server stays quiet
    _patch_websockets_connect(monkeypatch, lambda: fake_ws)

    # Run _run_ws as a background task in THIS event loop (bypassing the
    # service's normal asyncio.run-in-a-thread plumbing). resume() then
    # toggles the active event, allowing the loop to attempt connect.
    run_task = asyncio.create_task(svc._run_ws())
    # Let _run_ws bind _loop and _active_event before resume() probes them
    await asyncio.sleep(0.01)
    svc.resume()
    # Give the loop time to fire connect + send config + emit CONNECTED
    await asyncio.sleep(0.05)

    # Verify state + events
    assert svc.connection_state == STTConnectionState.CONNECTED
    state_events = [p["state"] for n, p in events if n == "ws_status"]
    assert "connecting" in state_events
    assert "connected" in state_events

    # Config message was sent on the wire
    assert len(fake_ws.sent_frames) == 1
    sent = json.loads(fake_ws.sent_frames[0])
    assert sent == {"probe": True, "sr": 16000}

    # Clean shutdown so the test doesn't leave a coroutine hanging
    svc.shutdown()
    try:
        await asyncio.wait_for(run_task, timeout=1.0)
    except asyncio.TimeoutError:
        run_task.cancel()


@pytest.mark.asyncio
async def test_pause_after_connected_closes_ws_and_idles(monkeypatch):
    """pause() forces the live WS closed → outer loop sees clear active
    event → emits IDLE without reconnecting."""

    svc = _ProbeBackend()
    events: list[tuple[str, dict]] = []
    svc.set_hook(lambda n, p: events.append((n, p)))

    fake_ws = _FakeWS(server_msgs=[])
    _patch_websockets_connect(monkeypatch, lambda: fake_ws)

    run_task = asyncio.create_task(svc._run_ws())
    await asyncio.sleep(0.01)
    svc.resume()
    await asyncio.sleep(0.05)
    assert svc.connection_state == STTConnectionState.CONNECTED

    events.clear()
    svc.pause()
    await asyncio.sleep(0.1)

    assert svc.connection_state == STTConnectionState.IDLE
    state_events = [p["state"] for n, p in events if n == "ws_status"]
    assert "idle" in state_events

    svc.shutdown()
    try:
        await asyncio.wait_for(run_task, timeout=1.0)
    except asyncio.TimeoutError:
        run_task.cancel()


@pytest.mark.asyncio
async def test_consecutive_failures_drives_to_error_state(monkeypatch):
    """ERROR_CONSECUTIVE_FAILURES connection exceptions in a row → state
    transitions to ERROR. Service keeps trying (the loop doesn't exit) so
    a transient outage that recovers will heal — but the chip surfaces
    the problem to the user.
    """
    svc = _ProbeBackend()
    events: list[tuple[str, dict]] = []
    svc.set_hook(lambda n, p: events.append((n, p)))

    # Every connect raises; backoff capped so the test runs fast.
    @asynccontextmanager
    async def always_fail(*args, **kwargs):
        raise ConnectionRefusedError("no server")
        yield  # pragma: no cover

    import services.stt_backends._ws_streaming as mod
    monkeypatch.setattr(mod, "BACKOFF_INITIAL", 0.005)
    monkeypatch.setattr(mod, "BACKOFF_MAX", 0.01)

    import websockets
    monkeypatch.setattr(websockets, "connect", always_fail)

    run_task = asyncio.create_task(svc._run_ws())
    await asyncio.sleep(0.01)
    svc.resume()

    # Wait long enough for ERROR_CONSECUTIVE_FAILURES failures
    # (5 * 0.01s backoff cap = ~50 ms plus overhead)
    for _ in range(20):
        await asyncio.sleep(0.05)
        if svc.connection_state == STTConnectionState.ERROR:
            break

    assert svc.connection_state == STTConnectionState.ERROR, (
        f"Expected ERROR state after {ERROR_CONSECUTIVE_FAILURES} failures, "
        f"got {svc.connection_state.value}"
    )
    # The service should still be alive — the loop keeps trying. is_alive
    # checks the worker thread, which doesn't exist in this test (we ran
    # _run_ws directly as a coroutine), so this is only about state.

    svc.shutdown()
    try:
        await asyncio.wait_for(run_task, timeout=1.0)
    except asyncio.TimeoutError:
        run_task.cancel()


@pytest.mark.asyncio
async def test_resume_after_shutdown_is_noop(monkeypatch):
    """resume() after shutdown() must not re-open the connection. The
    contract is permanent destruction."""
    svc = _ProbeBackend()
    events: list[tuple[str, dict]] = []
    svc.set_hook(lambda n, p: events.append((n, p)))

    svc.shutdown()
    events.clear()
    svc.resume()  # Should warn-and-return, NOT crash, NOT emit CONNECTING
    state_events = [p["state"] for n, p in events if n == "ws_status"]
    assert "connecting" not in state_events
