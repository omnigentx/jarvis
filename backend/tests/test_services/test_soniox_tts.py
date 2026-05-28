"""Soniox TTS provider — config, error handling, build_provider factory.

The real WebSocket roundtrip is exercised against a stand-in fake socket
(no network) so we can assert what bytes the client emits without
depending on Soniox or pulling the ``websockets`` event loop in tests.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import types

import pytest

from services.tts_backends import soniox as soniox_tts
from services.tts_backends.soniox import SonioxTTSProvider, build_provider


class _FakeWS:
    """Minimal duck-typed stand-in for ``websockets.WebSocketClientProtocol``.

    Records every ``send`` call so tests can assert the wire protocol, and
    replays a scripted sequence of server messages to drive the receive
    loop. The provider iterates the socket via ``async for msg in ws``,
    so we implement ``__aiter__``/``__anext__`` directly.
    """

    def __init__(self, server_messages):
        self.sent: list[str] = []
        self._server = list(server_messages)

    async def send(self, payload):
        if isinstance(payload, (bytes, bytearray)):
            self.sent.append(payload)
        else:
            self.sent.append(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._server:
            raise StopAsyncIteration
        return self._server.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _install_fake_websockets(monkeypatch, server_messages):
    """Replace the websockets module so ``import websockets`` returns a fake.

    The provider does ``import websockets`` inside ``_stream`` so we can't
    just patch a top-level binding — we have to install the module in
    ``sys.modules`` before the function runs.
    """
    captured: dict = {}
    fake_ws = _FakeWS(server_messages)

    def _connect(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return fake_ws

    fake_mod = types.SimpleNamespace(connect=_connect)
    monkeypatch.setitem(sys.modules, "websockets", fake_mod)
    return captured, fake_ws


@pytest.mark.asyncio
async def test_stream_audio_sends_config_then_text_and_decodes_base64(monkeypatch):
    chunk1 = b"\x00\x01\x02"
    chunk2 = b"\x03\x04"
    server = [
        json.dumps({"audio": base64.b64encode(chunk1).decode()}),
        json.dumps({"audio": base64.b64encode(chunk2).decode(), "audio_end": True}),
        json.dumps({"terminated": True}),
    ]
    captured, fake_ws = _install_fake_websockets(monkeypatch, server)

    provider = SonioxTTSProvider(api_key="sk-fake", voice="Adrian", sample_rate=24000)
    out: list[bytes] = []
    async for c in provider.stream_audio("hello"):
        out.append(c)

    assert b"".join(out) == chunk1 + chunk2
    assert captured["url"] == soniox_tts.SONIOX_TTS_WS_URL

    # First send is the config JSON, second is the text JSON.
    cfg = json.loads(fake_ws.sent[0])
    assert cfg["api_key"] == "sk-fake"
    assert cfg["voice"] == "Adrian"
    assert cfg["audio_format"] == "mp3"
    assert cfg["sample_rate"] == 24000

    text_msg = json.loads(fake_ws.sent[1])
    assert text_msg["text"] == "hello"
    assert text_msg["text_end"] is True
    assert text_msg["stream_id"] == cfg["stream_id"]


@pytest.mark.asyncio
async def test_stream_pcm_uses_pcm_audio_format(monkeypatch):
    server = [
        json.dumps({"audio": base64.b64encode(b"pcm").decode()}),
        json.dumps({"terminated": True}),
    ]
    _, fake_ws = _install_fake_websockets(monkeypatch, server)

    provider = SonioxTTSProvider(api_key="sk-fake")
    out: list[bytes] = []
    async for c in provider.stream_pcm("hi"):
        out.append(c)
    assert out == [b"pcm"]
    cfg = json.loads(fake_ws.sent[0])
    assert cfg["audio_format"] == "pcm_s16le"


@pytest.mark.asyncio
async def test_error_response_raises_runtime_error(monkeypatch):
    server = [json.dumps({
        "error_code": 401,
        "error_message": "invalid api key",
        "stream_id": "x",
    })]
    _install_fake_websockets(monkeypatch, server)

    provider = SonioxTTSProvider(api_key="sk-bad")
    with pytest.raises(RuntimeError, match="invalid api key"):
        async for _ in provider.stream_audio("hi"):
            pytest.fail("should not yield audio after server error")


@pytest.mark.asyncio
async def test_empty_text_yields_nothing(monkeypatch):
    # No fake server needed — empty-text short-circuits before connect.
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(
        connect=lambda *a, **kw: pytest.fail("connect should not be called"),
    ))
    provider = SonioxTTSProvider(api_key="sk-fake")
    chunks = [c async for c in provider.stream_audio("   ")]
    assert chunks == []


def test_build_provider_requires_api_key():
    with pytest.raises(RuntimeError, match="no API key"):
        build_provider({}, secrets={})


def test_build_provider_passes_params_through():
    provider = build_provider(
        {"voice": "Bella", "language": "vi", "model": "tts-rt-v1", "sample_rate": 48000},
        secrets={"api_key": "sk-fake"},
    )
    assert provider._voice == "Bella"
    assert provider._language == "vi"
    assert provider._sample_rate == 48000
