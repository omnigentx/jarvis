"""/ws/voice — bidirectional real-time voice transport.

The browser opens ONE WebSocket per hands-free session. Frames:

* **Client → Server (binary)**: 16 kHz mono int16 PCM frames captured by an
  AudioWorklet.
* **Client → Server (text JSON)**: control messages —
    - ``{"type":"start"}`` accepted-and-ignored (handshake)
    - ``{"type":"stop"}`` close session
    - ``{"type":"barge_in"}`` cancel in-flight agent + TTS
    - ``{"type":"speak","text":"..."}`` speak arbitrary text (manual probe)
    - ``{"type":"set_session","id":"..."}`` resume an existing chat session
* **Server → Client (text JSON)**: STT events (``partial_transcript``,
  ``stable_transcript``, ``final_transcript``, ``vad_start``, ``vad_stop``,
  ``recording_start``, ``recording_stop``, ``wake_word``), TTS events
  (``tts_start``, ``tts_end``, ``tts_interruption``, ``barge_in_ack``),
  conversation events (``user_message``, ``assistant_message``,
  ``session``, ``error``), lifecycle events (``stt_loading``, ``stt_ready``).
* **Server → Client (binary)**: TTS audio bytes (raw PCM 24 kHz int16 for
  RealtimeTTS engines, MP3 for the legacy Edge provider — client sniffs).

Conversation flow (RealtimeVoiceChat-inspired):

1. Browser captures mic → PCM → WS in.
2. STT recorder runs ``recorder.text()`` in a daemon loop, drives VAD.
3. ``recording_start`` fires → user is talking → cancel any in-flight
   agent reply + TTS (barge-in).
4. ``post_speech_silence_duration`` (0.7s default) of silence → recorder
   produces a final transcript via ``on_final``.
5. Final transcript → submit to fast-agent via ``session_service.resume_and_send``.
6. Agent reply → echo as ``assistant_message`` event + ``speak()`` over WS.
7. Loop.

Why one socket: half-duplex would force tracking two ids; a single socket
keeps barge-in latency low — VAD start instantly cancels in-process
without a round-trip.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
import wave
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services import shared_state as state


def _chunk_rms_peak(pcm_chunk: bytes) -> tuple[int, int]:
    """Tiny RMS+peak calculator for diagnostic logging only."""
    if not pcm_chunk or len(pcm_chunk) < 2:
        return 0, 0
    n = len(pcm_chunk) // 2
    samples = struct.unpack(f"<{n}h", pcm_chunk[: n * 2])
    if not samples:
        return 0, 0
    sq = sum(s * s for s in samples) / n
    return int(sq ** 0.5), max(abs(s) for s in samples)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["voice-ws"])


# Sample rate the dashboard's playback worklet expects on the PCM path.
# Must match useVoiceSession.js::PCM_PLAYBACK_RATE.
_TTS_PCM_RATE = 24000


async def _mp3_stream_to_pcm(mp3_iter):
    """Pipe an async iterator of MP3 byte chunks through ffmpeg and yield
    raw int16 mono PCM at ``_TTS_PCM_RATE``.

    Browsers can only decode complete MP3 frames; Edge-tts emits chunks
    smaller than a frame so per-chunk ``decodeAudioData`` glitches. By
    transcoding server-side to PCM we sidestep MP3 framing on the wire
    and let the worklet play a uniform PCM stream.

    The subprocess is created lazily on first use and torn down via the
    ``finally`` block on every path (cancellation included) so we don't
    leak ffmpeg processes when the user barges in.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel", "error",
        "-f", "mp3", "-i", "pipe:0",
        "-f", "s16le", "-ac", "1", "-ar", str(_TTS_PCM_RATE),
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def _pump_in() -> None:
        try:
            async for chunk in mp3_iter:
                if not chunk:
                    continue
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (asyncio.CancelledError, BrokenPipeError, ConnectionResetError):
            return
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    pump = asyncio.create_task(_pump_in())
    try:
        while True:
            buf = await proc.stdout.read(4096)
            if not buf:
                break
            yield buf
    finally:
        if not pump.done():
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):
                pass
        try:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
        except Exception:
            pass


@router.websocket("/ws/voice")
async def voice_ws(ws: WebSocket) -> None:
    """Handle a single hands-free session."""
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

    # Per-connection conversation state. Tasks are tracked so STT-driven
    # barge-in (recording_start while bot is mid-reply) can cancel both the
    # agent generation and the TTS playback in one step.
    session_id: Optional[str] = None
    agent_name: Optional[str] = None  # set via {"type":"set_agent","name":"..."}
    agent_task: Optional[asyncio.Task] = None
    tts_task: Optional[asyncio.Task] = None
    user_query_pending = False  # set when STT finalises text but agent task hasn't started yet
    bot_speaking = False        # True from tts_start to tts_end / interruption
    # AEC diagnostic — when bot is speaking, we copy incoming mic PCM into
    # this buffer + track RMS so we can prove whether echoCancellation is
    # actually scrubbing the loudspeaker bleed. Dumped to a WAV at tts_end.
    diag_dir = os.path.join("data", "voice_diag")
    os.makedirs(diag_dir, exist_ok=True)
    diag_during_bot: bytearray = bytearray()
    diag_silent_baseline_rms: list[int] = []  # RMS while bot off + user silent
    diag_during_bot_rms: list[int] = []        # RMS while bot speaking

    def _cancel_inflight(reason: str) -> None:
        """Abort agent generation + TTS playback. Idempotent."""
        nonlocal agent_task, tts_task
        cancelled = []
        if agent_task is not None and not agent_task.done():
            agent_task.cancel()
            cancelled.append("agent")
        if tts_task is not None and not tts_task.done():
            tts_task.cancel()
            cancelled.append("tts")
        if cancelled:
            logger.info("[ws_voice] barge-in (%s) cancelled %s", reason, ",".join(cancelled))
            try:
                out_queue.put_nowait({"type": "tts_interruption", "reason": reason})
            except Exception:
                pass

    def on_stt_event(name: str, payload: dict[str, Any]) -> None:
        """Bridge STT callbacks (run on worker thread) into the WS event queue."""
        nonlocal user_query_pending
        try:
            loop.call_soon_threadsafe(out_queue.put_nowait, {"type": name, **payload})
        except Exception:
            logger.exception("[ws_voice] failed to enqueue STT event %s", name)

        # Barge-in: a fresh recording starts while the bot is mid-reply.
        # We use ``recording_start`` (RealtimeSTT's "VAD locked-on" signal)
        # rather than ``vad_start`` because the latter can fire on background
        # noise; recording_start has webrtc + silero agreement on speech.
        if name == "recording_start" and bot_speaking:
            loop.call_soon_threadsafe(_cancel_inflight, "user_resumed")

        # Mark a user turn as ready to dispatch on the asyncio side. The
        # actual scheduling happens in the main receive loop so we stay on
        # the event-loop thread for create_task.
        if name == "final_transcript":
            text = (payload or {}).get("text") or ""
            if text.strip():
                user_query_pending = True
                loop.call_soon_threadsafe(_dispatch_user_turn, text.strip())

    def _dispatch_user_turn(text: str) -> None:
        """Fire-and-forget: schedule a coroutine that handles one full turn."""
        nonlocal agent_task, user_query_pending
        user_query_pending = False
        # Cancel any previous in-flight agent/tts so a barge-in mid-turn
        # cleanly resets to the new user input.
        if (agent_task is not None and not agent_task.done()) or (
            tts_task is not None and not tts_task.done()
        ):
            _cancel_inflight("new_user_turn")
        agent_task = asyncio.create_task(_handle_user_turn(text))

    async def writer() -> None:
        """Drain the event queue → WebSocket. JSON dicts → text frames; raw
        bytes/bytearray → binary frames."""
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

    # Start writer FIRST so events queued during the (slow) lazy STT init
    # below actually reach the client.
    writer_task = asyncio.create_task(writer())

    # Lazy STT init: faster-whisper + multiprocessing workers are heavy
    # (~3–10s cold start, +75 MB whisper + 50 MB silero on first run). We
    # announce status events so the UI shows "Loading STT model…" rather
    # than pretending to listen.
    stt_service = state.stt_recorder
    if stt_service is None:
        await out_queue.put({"type": "stt_loading"})
        try:
            from services.runtime_config import apply_voice_stt_config
            await asyncio.to_thread(apply_voice_stt_config, None)
            stt_service = state.stt_recorder
        except Exception as exc:
            logger.exception("[ws_voice] STT init failed")
            await out_queue.put({"type": "error", "detail": f"STT init failed: {exc}"})
        else:
            await out_queue.put({"type": "stt_ready"})

    if stt_service is not None:
        stt_service.set_hook(on_stt_event)

    async def speak(text: str) -> None:
        """Stream TTS audio chunks back over the socket. Cancellable.

        Why we always emit PCM (not MP3) on the WS path: browsers can only
        decode complete MP3 frames via ``decodeAudioData``. Edge-tts emits
        small chunks that often split a frame mid-byte → per-chunk decode
        produces gaps and glitches. We pipe MP3 → ffmpeg → s16le 24 kHz
        mono PCM so the front-end always sees a uniform PCM stream the
        AudioWorklet path can play seamlessly. RealtimeTTSProvider
        engines already emit PCM via ``stream_pcm`` so we use that
        directly when available (skip the ffmpeg hop).
        """
        nonlocal bot_speaking
        provider = state.tts_chat_provider
        stream_pcm = getattr(provider, "stream_pcm", None)
        bot_speaking = True
        await out_queue.put({"type": "tts_start"})
        try:
            if stream_pcm is not None:
                async for pcm in stream_pcm(text):
                    await out_queue.put(pcm)
            else:
                async for pcm in _mp3_stream_to_pcm(provider.stream_audio(text)):
                    await out_queue.put(pcm)
        except asyncio.CancelledError:
            await out_queue.put({"type": "barge_in_ack"})
            raise
        finally:
            bot_speaking = False
            # Dump the mic PCM captured during this TTS turn to /data/voice_diag
            # so we can listen to what the server saw + compare RMS vs the
            # silent baseline. This is the ground truth for "is AEC working".
            try:
                if diag_during_bot:
                    fname = f"during_bot_{int(time.time())}.wav"
                    path = os.path.join(diag_dir, fname)
                    with wave.open(path, "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(16000)
                        w.writeframes(bytes(diag_during_bot))
                    bot_rms = diag_during_bot_rms or [0]
                    silent = diag_silent_baseline_rms or [0]
                    logger.info(
                        "[ws_voice diag] mic during bot: chunks=%d avg_rms=%d max_rms=%d "
                        "| silent baseline: chunks=%d avg_rms=%d max_rms=%d "
                        "| dumped %s",
                        len(bot_rms), sum(bot_rms) // max(1, len(bot_rms)), max(bot_rms),
                        len(silent), sum(silent) // max(1, len(silent)), max(silent),
                        path,
                    )
            except Exception:
                logger.exception("[ws_voice diag] dump failed")
            finally:
                diag_during_bot.clear()
                diag_during_bot_rms.clear()
                diag_silent_baseline_rms.clear()
            await out_queue.put({"type": "tts_end"})

    async def _handle_user_turn(text: str) -> None:
        """One turn of the conversation: echo user → invoke agent → speak.

        Event order chosen so the dashboard can render an "agent is
        thinking" placeholder synchronously with the user message:

            user_message → agent_thinking → (resume_and_send) →
            session (if new) → assistant_message → tts_start → audio chunks
            → tts_end

        Cancellation policy: when ``recording_start`` fires while we're in
        flight, the outer scheduler cancels this task. CancelledError
        propagates up; the writer is already draining tts_interruption and
        the next user_turn task replaces this one.
        """
        nonlocal session_id, tts_task
        await out_queue.put({"type": "user_message", "text": text})
        try:
            if state.agent_app is None:
                await out_queue.put({"type": "error", "detail": "Agent runtime not ready"})
                return
            # Frontend listens for agent_thinking to flip the chat-message
            # placeholder into a streaming spinner state — without this the
            # UI looks frozen for the 1–10s the LLM takes to start replying.
            await out_queue.put({"type": "agent_thinking"})

            # Voice + chat both send the user text raw — voice formatting
            # comes from the chosen TTS provider (configurable in Settings).
            response, new_session_id = await state.session_service.resume_and_send(
                state.agent_app,
                text,
                session_id,
                agent_name=agent_name,
            )
            if new_session_id and new_session_id != session_id:
                session_id = new_session_id
                await out_queue.put({"type": "session", "id": session_id})
            response = (response or "").strip()
            if not response:
                # Empty reply — finalize placeholder with a clear message so
                # the UI doesn't sit on an invisible streaming bubble.
                await out_queue.put({
                    "type": "assistant_message",
                    "text": "",
                    "session_id": session_id,
                    "empty": True,
                })
                return
            await out_queue.put({
                "type": "assistant_message",
                "text": response,
                "session_id": session_id,
            })
            tts_task = asyncio.create_task(speak(response))
            try:
                await tts_task
            except asyncio.CancelledError:
                # speak() already pushed barge_in_ack + tts_interruption.
                # Don't re-raise — the user_turn itself completed; only the
                # audio playback was cut. Returning normally lets the next
                # user turn schedule cleanly.
                return
        except asyncio.CancelledError:
            # Cancelled before / during agent generation. Surface a clear
            # signal so the placeholder can show as interrupted instead of
            # spinning forever.
            try:
                out_queue.put_nowait({"type": "tts_interruption", "reason": "user_resumed"})
            except Exception:
                pass
            raise
        except Exception as exc:
            logger.exception("[ws_voice] agent turn failed")
            await out_queue.put({"type": "error", "detail": f"agent error: {exc}"})

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if "bytes" in msg and msg["bytes"] is not None:
                # Diagnostic: classify each frame as "during bot" vs "silent
                # baseline" so we can compare RMS distributions and confirm
                # whether browser AEC is actually scrubbing the bot's voice
                # before STT sees it. Lightweight enough to leave on; the
                # WAV dump at tts_end is the conclusive evidence.
                rms, peak = _chunk_rms_peak(msg["bytes"])
                if bot_speaking:
                    diag_during_bot.extend(msg["bytes"])
                    diag_during_bot_rms.append(rms)
                else:
                    if rms < 200:  # only collect "user not talking" frames
                        diag_silent_baseline_rms.append(rms)
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
                    # Manual probe — bypass STT, useful for testing TTS path.
                    text = payload.get("text") or ""
                    if tts_task is not None and not tts_task.done():
                        tts_task.cancel()
                    tts_task = asyncio.create_task(speak(text))
                elif kind == "barge_in":
                    _cancel_inflight("client_barge_in")
                elif kind == "set_session":
                    session_id = payload.get("id") or session_id
                elif kind == "set_agent":
                    # Lets the dashboard tell us which agent the user picked
                    # in the chat UI (default Jarvis if unset).
                    agent_name = payload.get("name") or agent_name
                elif kind == "diag":
                    # Frontend reports its capture-path settings (track
                    # constraints actually applied + AudioContext sampleRate).
                    # We log them so the dump's RMS numbers can be interpreted
                    # in context.
                    logger.info("[ws_voice diag] frontend report: %s", payload)
                elif kind == "stop":
                    return
                # "start" is a noop — accepting the socket already starts
    except WebSocketDisconnect:
        pass
    finally:
        if stt_service is not None:
            stt_service.set_hook(None)
        for t in (agent_task, tts_task):
            if t is not None and not t.done():
                t.cancel()
        await out_queue.put(None)
        writer_task.cancel()
        try:
            await writer_task
        except (asyncio.CancelledError, Exception):
            pass
