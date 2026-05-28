"""End-to-end Soniox WebSocket integration test against a real loopback server.

What this guards (per CLAUDE.md rule 6 — Happy Path FIRST):
* The full async path through ``SonioxSTTService`` and ``SonioxTTSProvider``
  — TCP connect, JSON handshake, binary audio frames, JSON token frames,
  base64-decoded audio chunks, clean ``terminated``-driven shutdown.
* The WS scaffolding in :class:`WSStreamingSTT` (thread runner, audio
  queue, hook fan-out, end-of-audio empty-string sentinel) is exercised
  through the same code path production uses.

What this does NOT guard:
* The real Soniox API. Hitting Soniox in CI would require credentials and
  cost real money on every run. We stand up a local ``websockets.serve``
  on an ephemeral port and patch ``SONIOX_*_WS_URL`` to point at it. The
  fake server speaks the documented Soniox protocol — config message in,
  tokens / audio out, ``terminated`` to close.

If Soniox changes their wire format, these tests still pass — that's the
fake-server tradeoff. They guard *our* client code, not the protocol
contract. The unit tests in test_soniox_stt.py / test_soniox_tts.py
codify the documented protocol shape against snapshots so a Soniox API
break can be detected at the spec level even without live calls.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest
import websockets

from services.stt_backends import soniox as soniox_stt
from services.tts_backends import soniox as soniox_tts


# ---- Shared fake-server scaffolding ---------------------------------------


class _FakeServer:
    """Run a websockets server on a free port and expose its URL."""

    def __init__(self, handler):
        self._handler = handler
        self._server: websockets.Server | None = None
        self._task: asyncio.Task | None = None
        self.url: str = ""

    async def start(self):
        # Port 0 = pick a free ephemeral port; serve only on localhost so the
        # test never accidentally listens on a public interface.
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        host, port = self._server.sockets[0].getsockname()[:2]
        self.url = f"ws://{host}:{port}"
        return self

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


# ---- STT end-to-end --------------------------------------------------------


def _hook_recorder():
    events: list[tuple[str, dict]] = []
    return events, (lambda ev, payload: events.append((ev, payload)))


@pytest.mark.asyncio
async def test_soniox_stt_full_websocket_roundtrip(monkeypatch):
    """Real WS handshake → PCM bytes → tokens back → final_transcript event."""
    received_config: dict = {}
    received_audio: list[bytes] = []
    end_of_audio = asyncio.Event()

    async def handler(ws):
        # First message is the JSON config.
        cfg_msg = await ws.recv()
        received_config.update(json.loads(cfg_msg))
        # Subsequent messages are binary audio frames until the client sends
        # an empty string sentinel.
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                received_audio.append(bytes(msg))
                # Once we've seen audio, stream a couple of token frames so
                # the client sees both provisional + final + endpoint cases.
                if len(received_audio) == 1:
                    await ws.send(json.dumps({"tokens": [
                        {"text": "Xin ", "is_final": True},
                        {"text": "chào", "is_final": False},
                    ]}))
                if len(received_audio) == 2:
                    await ws.send(json.dumps({"tokens": [
                        {"text": "chào ", "is_final": True},
                        {"text": "Jarvis", "is_final": True},
                        {"text": "<end>", "is_final": True},
                    ]}))
            elif msg == "":
                end_of_audio.set()
                await ws.send(json.dumps({"finished": True}))
                return

    server = await _FakeServer(handler).start()
    # WS_URL is a ClassVar — patching the module-level constant won't
    # touch the class attribute that's already been copied at definition
    # time, so we patch the class directly.
    monkeypatch.setattr(soniox_stt.SonioxSTTService, "WS_URL", server.url)

    events, hook = _hook_recorder()
    svc = soniox_stt.SonioxSTTService(api_key="sk-fake", params={
        "model": "stt-rt-v4", "language_hints": "vi,en",
    })
    svc.set_hook(hook)
    svc.start_listen_loop()

    # Give the WS thread a beat to connect + send config. The handler keeps
    # us honest: the server only registers the config once the handshake
    # completes, so we poll the dict instead of guessing a sleep duration.
    deadline = time.monotonic() + 5.0
    while not received_config and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert received_config, "server never received the config message"
    assert received_config["api_key"] == "sk-fake"
    assert received_config["model"] == "stt-rt-v4"
    assert received_config["audio_format"] == "pcm_s16le"
    assert received_config["sample_rate"] == 16000
    assert received_config["language_hints"] == ["vi", "en"]
    assert received_config["enable_endpoint_detection"] is True

    # Feed two PCM frames — the handler emits tokens after each so we can
    # observe partial → final → endpoint progression.
    svc.feed_audio(b"\x00" * 320)  # 10ms of silence at 16 kHz int16 mono
    svc.feed_audio(b"\x01" * 320)

    # Wait for the endpoint token to flush the final transcript.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if any(ev == "final_transcript" for ev, _ in events):
            break
        await asyncio.sleep(0.02)

    try:
        finals = [p["text"] for ev, p in events if ev == "final_transcript"]
        assert finals == ["Xin chào Jarvis"], (
            f"expected one final transcript 'Xin chào Jarvis', got {finals!r}"
        )

        # The provisional partial showed up before the endpoint cleared the
        # buffer. The exact intermediate value depends on token timing, but
        # the running partial must contain at least the first final token.
        partials = [p["text"] for ev, p in events if ev == "partial_transcript"]
        assert partials, "no partial_transcript emitted"
        assert any("Xin" in p for p in partials), partials

        # Endpoint emits the vad_stop → recording_stop → recording_start
        # triplet that downstream consumers rely on.
        names = [ev for ev, _ in events]
        assert "vad_stop" in names
        assert "recording_stop" in names
    finally:
        svc.shutdown()
        await server.stop()


@pytest.mark.asyncio
async def test_soniox_stt_surfaces_server_error_event(monkeypatch):
    """A server-side ``error_code`` lands as an ``error`` event on the hook."""
    async def handler(ws):
        await ws.recv()  # discard config
        await ws.send(json.dumps({
            "error_code": 401,
            "error_message": "invalid api key",
        }))
        # Keep the socket open so the receiver actually sees the error
        # before the server-driven close — closing too quickly races the
        # receive loop and the test would flake.
        await asyncio.sleep(0.1)

    server = await _FakeServer(handler).start()
    # WS_URL is a ClassVar — patching the module-level constant won't
    # touch the class attribute that's already been copied at definition
    # time, so we patch the class directly.
    monkeypatch.setattr(soniox_stt.SonioxSTTService, "WS_URL", server.url)

    events, hook = _hook_recorder()
    svc = soniox_stt.SonioxSTTService(api_key="sk-bad", params={})
    svc.set_hook(hook)
    svc.start_listen_loop()

    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if any(ev == "error" for ev, _ in events):
                break
            await asyncio.sleep(0.02)
        errors = [p["detail"] for ev, p in events if ev == "error"]
        assert errors and "invalid api key" in errors[0]
    finally:
        svc.shutdown()
        await server.stop()


# ---- TTS end-to-end --------------------------------------------------------


@pytest.mark.asyncio
async def test_soniox_tts_full_websocket_roundtrip(monkeypatch):
    """Real WS handshake → text frame → base64 audio chunks → terminated."""
    received_msgs: list[dict] = []

    async def handler(ws):
        # The TTS protocol is JSON-only — config first, then text frames.
        async for raw in ws:
            data = json.loads(raw)
            received_msgs.append(data)
            if "text" in data and data.get("text_end"):
                # Stream two audio chunks then close cleanly.
                stream_id = data["stream_id"]
                await ws.send(json.dumps({
                    "audio": base64.b64encode(b"chunk-one").decode(),
                    "stream_id": stream_id,
                }))
                await ws.send(json.dumps({
                    "audio": base64.b64encode(b"chunk-two").decode(),
                    "audio_end": True,
                    "stream_id": stream_id,
                }))
                await ws.send(json.dumps({
                    "terminated": True, "stream_id": stream_id,
                }))
                return

    server = await _FakeServer(handler).start()
    monkeypatch.setattr(soniox_tts, "SONIOX_TTS_WS_URL", server.url)

    provider = soniox_tts.SonioxTTSProvider(
        api_key="sk-fake", voice="Adrian", language="vi", sample_rate=24000,
    )
    out: list[bytes] = []
    async for chunk in provider.stream_audio("Xin chào"):
        out.append(chunk)

    try:
        assert b"".join(out) == b"chunk-one" + b"chunk-two"
        # First inbound is the config (api_key + voice + audio_format), second
        # is the text payload with text_end=True.
        assert len(received_msgs) == 2
        cfg, text_msg = received_msgs
        assert cfg["api_key"] == "sk-fake"
        assert cfg["voice"] == "Adrian"
        assert cfg["language"] == "vi"
        assert cfg["audio_format"] == "mp3"
        assert cfg["sample_rate"] == 24000
        assert cfg["model"] == "tts-rt-v1"
        assert "stream_id" in cfg
        assert text_msg["text"] == "Xin chào"
        assert text_msg["text_end"] is True
        assert text_msg["stream_id"] == cfg["stream_id"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_soniox_tts_pcm_path_requests_pcm_format(monkeypatch):
    """``stream_pcm`` swaps audio_format to ``pcm_s16le`` for the WS path."""
    received_cfg: dict = {}

    async def handler(ws):
        async for raw in ws:
            data = json.loads(raw)
            if "api_key" in data:
                received_cfg.update(data)
            elif data.get("text_end"):
                await ws.send(json.dumps({
                    "audio": base64.b64encode(b"\x00\x01").decode(),
                    "audio_end": True,
                    "stream_id": data["stream_id"],
                }))
                await ws.send(json.dumps({
                    "terminated": True, "stream_id": data["stream_id"],
                }))
                return

    server = await _FakeServer(handler).start()
    monkeypatch.setattr(soniox_tts, "SONIOX_TTS_WS_URL", server.url)

    provider = soniox_tts.SonioxTTSProvider(api_key="sk-fake")
    out: list[bytes] = []
    async for chunk in provider.stream_pcm("hello"):
        out.append(chunk)

    try:
        assert b"".join(out) == b"\x00\x01"
        assert received_cfg["audio_format"] == "pcm_s16le"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_soniox_tts_error_response_raises(monkeypatch):
    """Server ``error_code`` surfaces as a ``RuntimeError`` to the caller."""
    async def handler(ws):
        async for raw in ws:
            data = json.loads(raw)
            if data.get("text_end"):
                await ws.send(json.dumps({
                    "error_code": 402,
                    "error_message": "quota exceeded",
                    "stream_id": data["stream_id"],
                }))
                return

    server = await _FakeServer(handler).start()
    monkeypatch.setattr(soniox_tts, "SONIOX_TTS_WS_URL", server.url)

    provider = soniox_tts.SonioxTTSProvider(api_key="sk-fake")
    try:
        with pytest.raises(RuntimeError, match="quota exceeded"):
            async for _ in provider.stream_audio("hi"):
                pytest.fail("should not yield audio after error")
    finally:
        await server.stop()
