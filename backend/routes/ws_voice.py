"""/ws/voice — bidirectional real-time voice transport.

The browser opens ONE WebSocket per hands-free session. Frames:

* **Client → Server (binary)**: 16 kHz mono int16 PCM frames captured by an
  AudioWorklet.
* **Client → Server (text JSON)**: control messages — ``{"type":"start"}``,
  ``{"type":"stop"}``, ``{"type":"barge_in"}``.
* **Server → Client (text JSON)**: events emitted by the STT pipeline
  (``partial_transcript``, ``stable_transcript``, ``final_transcript``,
  ``vad_start``, ``vad_stop``, ``wake_word``) plus TTS events
  (``tts_start``, ``tts_end``, ``barge_in_ack``).
* **Server → Client (binary)**: TTS PCM frames (24 kHz mono int16) for the
  Web Audio AudioWorklet to enqueue and play.

Why one socket: half-duplex would force us to track two ids; one socket
keeps barge-in latency low — VAD start instantly cancels TTS in-process
without a round-trip.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services import shared_state as state

logger = logging.getLogger(__name__)
router = APIRouter(tags=["voice-ws"])


@router.websocket("/ws/voice")
async def voice_ws(ws: WebSocket) -> None:
    """Handle a single hands-free session.

    Auth note: WebSocket auth follows the existing pattern — the client passes
    ``?api_key=...`` query param. We honor the same secret as REST auth.
    """
    await ws.accept()

    # Soft auth: skip in dev (no JARVIS_API_KEY), enforce when set.
    import os
    expected = os.environ.get("JARVIS_API_KEY")
    if expected:
        token = ws.query_params.get("api_key")
        if token != expected:
            await ws.close(code=4401)
            return

    loop = asyncio.get_running_loop()
    out_queue: asyncio.Queue = asyncio.Queue()
    tts_task: Optional[asyncio.Task] = None
    stt_service = state.stt_recorder  # may be None until first config

    def on_stt_event(name: str, payload: dict[str, Any]) -> None:
        """Bridge STT callbacks (run on worker thread) into the WS event queue."""
        nonlocal tts_task
        try:
            loop.call_soon_threadsafe(out_queue.put_nowait, {"type": name, **payload})
        except Exception:
            logger.exception("[ws_voice] failed to enqueue STT event %s", name)
        # Barge-in: the moment the user starts speaking, cancel any in-flight TTS.
        if name == "vad_start" and tts_task is not None and not tts_task.done():
            tts_task.cancel()

    if stt_service is not None:
        stt_service.set_hook(on_stt_event)

    async def writer() -> None:
        """Drain the event queue → WebSocket.

        We never block the STT worker thread on a slow client: queue is
        unbounded but events are small JSON dicts so backpressure is naturally
        bounded by the Whisper inference cadence.
        """
        try:
            while True:
                evt = await out_queue.get()
                if evt is None:
                    return
                if isinstance(evt, dict):
                    await ws.send_text(json.dumps(evt))
                elif isinstance(evt, (bytes, bytearray)):
                    await ws.send_bytes(evt)
        except WebSocketDisconnect:
            return

    writer_task = asyncio.create_task(writer())

    async def speak(text: str) -> None:
        """Stream TTS PCM chunks back over the socket. Cancellable for barge-in."""
        provider = state.tts_chat_provider
        # Only RealtimeTTSProvider exposes raw PCM; if the active engine is Edge
        # we fall back to MP3 chunks (browser <audio> rendering still works for
        # the post-response listen-back path).
        stream_pcm = getattr(provider, "stream_pcm", None)
        await out_queue.put({"type": "tts_start"})
        try:
            if stream_pcm is not None:
                async for pcm in stream_pcm(text):
                    await out_queue.put(pcm)
            else:
                async for mp3 in provider.stream_audio(text):
                    await out_queue.put(mp3)
        except asyncio.CancelledError:
            await out_queue.put({"type": "barge_in_ack"})
            raise
        finally:
            await out_queue.put({"type": "tts_end"})

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if "bytes" in msg and msg["bytes"] is not None:
                if stt_service is not None:
                    stt_service.feed_audio(msg["bytes"])
                continue
            if "text" in msg and msg["text"]:
                try:
                    payload = json.loads(msg["text"])
                except json.JSONDecodeError:
                    await out_queue.put({"type": "error", "detail": "invalid JSON"})
                    continue
                kind = payload.get("type")
                if kind == "speak":
                    text = payload.get("text") or ""
                    if tts_task is not None and not tts_task.done():
                        tts_task.cancel()
                    tts_task = asyncio.create_task(speak(text))
                elif kind == "barge_in":
                    if tts_task is not None and not tts_task.done():
                        tts_task.cancel()
                elif kind == "stop":
                    return
                # "start" is a noop — accepting the socket already starts
    except WebSocketDisconnect:
        pass
    finally:
        if stt_service is not None:
            stt_service.set_hook(None)
        if tts_task is not None and not tts_task.done():
            tts_task.cancel()
        await out_queue.put(None)
        writer_task.cancel()
        try:
            await writer_task
        except (asyncio.CancelledError, Exception):
            pass
