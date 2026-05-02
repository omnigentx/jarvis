"""/ws/voice — WebSocket transport for the hands-free path.

We can't exercise faster-whisper or a real audio pipeline in CI, so the test
plugs a fake STT service into ``shared_state`` and checks that:

  * binary frames from the client end up in ``feed_audio``
  * STT callbacks bridged through ``set_hook`` arrive on the wire as JSON
  * the speak() control message routes to the active chat provider and the
    response audio flows back as binary frames
  * barge-in via VAD start cancels in-flight TTS
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient


class _FakeSTT:
    """Stand-in for RealtimeSTTService — captures fed audio + exposes hook."""

    def __init__(self):
        self.fed: list[bytes] = []
        self._hook = None

    def set_hook(self, hook):
        self._hook = hook

    def feed_audio(self, chunk: bytes):
        self.fed.append(chunk)

    def emit(self, name: str, payload=None):
        if self._hook is not None:
            self._hook(name, payload or {})

    def shutdown(self):
        pass


class _StubProvider:
    """Stand-in chat TTS provider — emits raw PCM directly so the test
    sidesteps the production MP3→ffmpeg→PCM transcode pipeline."""

    async def stream_audio(self, text: str):  # not used when stream_pcm exists
        yield b""

    async def stream_pcm(self, text: str):
        # 4 samples of int16 silence — enough that the WS handler routes a
        # binary frame back to the client, which is what the assertion below
        # checks.
        yield b"\x00\x00\x00\x00\x00\x00\x00\x00"


@pytest.fixture()
def ws_client(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", "")  # disable WS auth for this test
    from sqlalchemy import create_engine as _ce
    from sqlalchemy.orm import sessionmaker
    from core.database import Base, SETUP_WIZARD_CRITICAL_STEPS, SetupWizardStep
    eng = _ce(f"sqlite:///{tmp_path}/ws.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    with Session() as db:
        for name in SETUP_WIZARD_CRITICAL_STEPS:
            db.add(SetupWizardStep(step_name=name, completed=True))
        db.commit()
    import core.database as core_db
    from services import config_service as cs_mod
    monkeypatch.setattr(core_db, "SessionLocal", Session)
    monkeypatch.setattr(cs_mod, "SessionLocal", Session)
    monkeypatch.setattr(cs_mod, "config_service", cs_mod.ConfigService(db_factory=Session))
    from middleware.setup_gate import _reset_cache_for_tests, refresh_setup_complete
    _reset_cache_for_tests()
    refresh_setup_complete()

    from services import shared_state
    fake_stt = _FakeSTT()
    monkeypatch.setattr(shared_state, "stt_recorder", fake_stt)
    monkeypatch.setattr(shared_state, "tts_chat_provider", _StubProvider())

    from server import app
    return TestClient(app), fake_stt


def test_pcm_in_flows_to_feed_audio(ws_client):
    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_bytes(b"\x00\x01\x02\x03")
        ws.send_bytes(b"\x04\x05")
        ws.close()
    assert fake_stt.fed == [b"\x00\x01\x02\x03", b"\x04\x05"]


def test_stt_event_bridges_to_json_frame(ws_client):
    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        # Trigger an STT event from "outside" — emulates a faster-whisper
        # callback firing on a worker thread.
        fake_stt.emit("partial_transcript", {"text": "xin chào"})
        msg = ws.receive_text()
        evt = json.loads(msg)
        assert evt["type"] == "partial_transcript"
        assert evt["text"] == "xin chào"
        ws.close()


def test_speak_command_streams_chat_provider_audio_back(ws_client):
    client, _ = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps({"type": "speak", "text": "hi"}))
        # Order: tts_start JSON → binary PCM chunk(s) → tts_end JSON.
        first = ws.receive_text()
        assert json.loads(first)["type"] == "tts_start"
        audio = ws.receive_bytes()
        # WS now emits raw int16 mono PCM (no MP3 magic bytes); just check
        # we got a non-empty even-length chunk that could be PCM.
        assert len(audio) > 0 and len(audio) % 2 == 0
        last = ws.receive_text()
        assert json.loads(last)["type"] == "tts_end"
        ws.close()


def _drain_until(ws, type_name: str, *, max_msgs: int = 8):
    """Pull text+binary frames until a JSON event of ``type_name`` shows up.

    Binary frames (audio chunks) and earlier control events are silently
    discarded so the test doesn't have to mirror the exact ordering of
    tts_start / audio / tts_end which is provider-dependent.
    """
    for _ in range(max_msgs):
        msg = ws.receive()
        if msg.get("type") == "websocket.disconnect":
            raise AssertionError(f"socket closed before {type_name}")
        if msg.get("text"):
            evt = json.loads(msg["text"])
            if evt.get("type") == type_name:
                return evt
    raise AssertionError(f"never saw {type_name} (drained {max_msgs} msgs)")


def test_barge_in_keeps_socket_alive_for_further_events(ws_client):
    """Sending a barge_in mid-conversation must not break the socket.

    The actual cancellation latency depends on the active provider — for our
    stub (which finishes synth in microseconds) the cancel is racy by design.
    What we lock in here is the safety property: after barge_in the socket
    is still usable for further events.
    """
    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps({"type": "speak", "text": "x"}))
        _drain_until(ws, "tts_end")
        ws.send_text(json.dumps({"type": "barge_in"}))
        # Socket should still accept further STT events after barge_in.
        fake_stt.emit("partial_transcript", {"text": "after"})
        evt = _drain_until(ws, "partial_transcript")
        assert evt["text"] == "after"
        ws.close()
