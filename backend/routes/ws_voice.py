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
  conversation events (``user_message``, ``agent_thinking``,
  ``assistant_message``, ``session``, ``error``), tool-call events
  forwarded from the chat progress hooks so the dashboard renders the
  same tool bubbles as text chat (``tool_request``, ``tool_done``,
  ``tool_running``), lifecycle events (``stt_loading``, ``stt_ready``).
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
import uuid
import wave
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.session import SESSION_COOKIE_NAME, SessionVerifyError, verify_session_token
from services import shared_state as state
from services.sse_progress import (
    create_progress_hooks,
    merge_hooks,
    progress_manager,
)


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


# ─── Auth helpers (cookie / Bearer / query — with CSWSH defence) ────────────


def _expected_ws_origin(ws: WebSocket) -> str:
    """Derive the origin a browser-initiated WS upgrade should declare.

    Mirrors ``core.webauthn.origin_from_request`` so the WS auth path
    and the WebAuthn RP-ID derivation agree on what "this deployment"
    means. Honors ``X-Forwarded-Host`` / ``X-Forwarded-Proto`` so
    reverse-proxy setups (Caddy, nginx, Tailscale Funnel) don't have
    to set anything extra.
    """
    forwarded_host = ws.headers.get("x-forwarded-host", "")
    host = (
        forwarded_host.split(",")[0].strip() if forwarded_host else ""
    ) or ws.headers.get("host", "")

    forwarded_proto = ws.headers.get("x-forwarded-proto", "")
    if "https" in forwarded_proto.lower() or ws.url.scheme in ("wss", "https"):
        scheme = "https"
    else:
        scheme = "http"
    return f"{scheme}://{host}"


def _ws_origin_allowed(ws: WebSocket) -> bool:
    """Origin check used on the cookie auth path (CSWSH defence).

    Modern browsers honor ``SameSite=Lax`` on WebSocket upgrades, but
    SameSite enforcement is browser-version-dependent and silently
    bypassed by proxies that strip cookie attributes. So when the
    cookie alone authorizes a WS, we additionally require the request
    declares an Origin matching the deployment.

    Allow-list:
      * The deployment's own origin (derived from request headers).
      * Any origin listed in ``TRUSTED_WS_ORIGINS`` (comma-separated)
        — for setups that proxy multiple front-ends to one backend.

    Missing-Origin rejection is intentional: browsers attach Origin
    on WS upgrade since at least 2014. A missing Origin on a cookie
    auth path is either a non-browser client (which should use Bearer
    or query instead) or an attacker stripping headers.
    """
    origin = ws.headers.get("origin", "")
    if not origin:
        return False
    if origin == _expected_ws_origin(ws):
        return True
    extra = os.environ.get("TRUSTED_WS_ORIGINS", "")
    if extra:
        allowed = {o.strip() for o in extra.split(",") if o.strip()}
        if origin in allowed:
            return True
    return False


def _ws_authenticated(ws: WebSocket, expected_api_key: str) -> bool:
    """Three-way auth for the voice WebSocket.

    Cookie path is the SPA's primary credential. Bearer + ``?api_key=``
    paths stay so the Xiaozhi voice device and any CLI scripts that
    drive this endpoint don't break.

    Cookie path additionally requires Origin to match (CSWSH defence
    — see ``_ws_origin_allowed``). Bearer + query bypass the Origin
    check because they're credential-of-possession that the browser
    cannot present cross-origin in the first place.

    Returns True on first credential that verifies. Logs which path
    won so cookie/Bearer/query divergence is diagnosable from server
    logs instead of tcpdump.
    """
    # 1) Session cookie (the SPA's primary path).
    raw_session = ws.cookies.get(SESSION_COOKIE_NAME)
    if raw_session:
        try:
            verify_session_token(raw_session)
            if not _ws_origin_allowed(ws):
                logger.warning(
                    "[WS-AUTH] cookie valid but Origin rejected: %s",
                    ws.headers.get("origin", "<missing>"),
                )
                # Don't short-circuit; the caller may still authenticate
                # via Bearer or query, which are CSWSH-safe.
            else:
                logger.debug("[WS-AUTH] accepted via session cookie")
                return True
        except SessionVerifyError as exc:
            # Cookie present but invalid (expired, key rotated, tampered).
            # Don't short-circuit — caller may have a legacy Bearer too.
            logger.debug("[WS-AUTH] session cookie rejected: %s", exc.reason)

    # 2) Authorization: Bearer header (programmatic clients).
    auth_header = ws.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token == expected_api_key:
            logger.debug("[WS-AUTH] accepted via Bearer header")
            return True

    # 3) ?api_key= query param (legacy — Xiaozhi device).
    token = ws.query_params.get("api_key", "")
    if token and token == expected_api_key:
        logger.debug("[WS-AUTH] accepted via ?api_key= query")
        return True

    return False


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
    # WebSockets cannot set custom headers from the browser, so we accept
    # three credentials in order — same precedence as ``verify_api_key``:
    #   1. ``jarvis_session`` cookie (preferred — browser auto-attaches it
    #      during the upgrade handshake; works for cookie-only SPA login).
    #   2. ``Authorization: Bearer ...`` header (programmatic clients).
    #   3. ``?api_key=...`` query param (legacy — Xiaozhi device + scripts
    #      that can't set headers).
    import os
    expected = os.environ.get("JARVIS_API_KEY")
    if expected:
        if not _ws_authenticated(ws, expected):
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
    # Dictation mode: client picked the bottom mic in ChatInput (press-to-talk
    # transcription) instead of the top hands-free button. In this mode the
    # STT pipeline runs as usual but ``final_transcript`` does NOT trigger
    # the LLM agent turn — the client is using transcripts to populate a
    # text input the user will edit + submit manually. The flag is flipped
    # by ``{"type":"start","mode":"dictation"}`` and never resets within a
    # session (one socket = one role).
    dictation_mode = False
    # AEC diagnostic — when bot is speaking, we copy incoming mic PCM into
    # this buffer + track RMS so we can prove whether echoCancellation is
    # actually scrubbing the loudspeaker bleed. Dumped to a WAV at tts_end.
    # Off by default; set JARVIS_VOICE_DIAG=1 to enable. Without the gate,
    # heavy voice usage piles up unbounded WAV files in data/voice_diag/.
    diag_enabled = os.environ.get("JARVIS_VOICE_DIAG", "").strip().lower() in ("1", "true", "yes")
    diag_dir = os.path.join("data", "voice_diag")
    if diag_enabled:
        os.makedirs(diag_dir, exist_ok=True)
    # Soft cap on the in-memory buffer so a hung TTS provider can't grow it
    # forever (16 kHz mono int16 → ~32 KB/s; 60 s cap ≈ 1.9 MB).
    _DIAG_BUFFER_MAX_BYTES = 60 * 16000 * 2
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
        #
        # Always log this transition so we can prove (in the log) WHY a
        # barge-in did or did not fire — the previous silent path made it
        # impossible to tell whether the VAD missed the user voice or
        # whether the cancellation logic just didn't run.
        if name == "recording_start":
            logger.info(
                "[ws_voice] recording_start received (bot_speaking=%s) → %s",
                bot_speaking,
                "barge-in" if bot_speaking else "no-op",
            )
            if bot_speaking:
                loop.call_soon_threadsafe(_cancel_inflight, "user_resumed")

        # Mark a user turn as ready to dispatch on the asyncio side. The
        # actual scheduling happens in the main receive loop so we stay on
        # the event-loop thread for create_task. The dictation gate lives
        # inside ``_dispatch_user_turn`` (single-threaded — asyncio loop
        # only) so the worker thread never reads the flag concurrently
        # with the receive loop's writes.
        if name == "final_transcript":
            text = (payload or {}).get("text") or ""
            # BARGE-IN-DIAG: log every final_transcript arrival so we can
            # prove from the log whether STT reached this point at all,
            # and whether the text was empty / dispatched / dropped.
            logger.info(
                "[ws_voice] final_transcript received len=%d preview=%r → %s",
                len(text), text[:60],
                "dispatch" if text.strip() else "drop(empty)",
            )
            if text.strip():
                user_query_pending = True
                loop.call_soon_threadsafe(_dispatch_user_turn, text.strip())

    def _dispatch_user_turn(text: str) -> None:
        """Fire-and-forget: schedule a coroutine that handles one full turn."""
        nonlocal agent_task, user_query_pending
        user_query_pending = False
        # BARGE-IN-DIAG: log dispatch entry + dictation gate + cancel state.
        agent_state = "none" if agent_task is None else ("done" if agent_task.done() else "active")
        tts_state = "none" if tts_task is None else ("done" if tts_task.done() else "active")
        logger.info(
            "[ws_voice] _dispatch_user_turn entry text=%r dictation=%s agent=%s tts=%s",
            text[:60], dictation_mode, agent_state, tts_state,
        )
        # Dictation gate. ``dictation_mode`` is set by the receive loop
        # (also asyncio thread) when the client sends
        # ``{type:'start', mode:'dictation'}``. Checking here keeps both
        # the read and the write on the event-loop thread, which is the
        # only way to make the gate race-free without a lock — the client
        # wants the raw transcript (already emitted upstream) so it can
        # populate a text box; skipping the LLM/TTS pipeline below is the
        # whole point of the flag.
        if dictation_mode:
            logger.info("[ws_voice] _dispatch_user_turn skipped (dictation mode)")
            return
        # Cancel any previous in-flight agent/tts so a barge-in mid-turn
        # cleanly resets to the new user input.
        if (agent_task is not None and not agent_task.done()) or (
            tts_task is not None and not tts_task.done()
        ):
            _cancel_inflight("new_user_turn")
        agent_task = asyncio.create_task(_handle_user_turn(text))
        logger.info("[ws_voice] _dispatch_user_turn scheduled new agent_task")

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
    #
    # Health probe before reuse: ``state.stt_recorder`` is a process-level
    # singleton. Pre-fix, a Soniox 408 idle timeout would leave the singleton
    # object alive with a dead inner WS — every subsequent /ws/voice connect
    # would reuse it and silently drop all audio (2026-05-29 incident). The
    # ``is_alive`` probe forces a rebuild when the singleton crashed
    # (thread died, transitioned to terminal ERROR after consecutive failures).
    stt_service = state.stt_recorder
    needs_rebuild = stt_service is None or not getattr(stt_service, "is_alive", True)
    if needs_rebuild:
        if stt_service is not None:
            logger.warning(
                "[ws_voice] STT singleton unhealthy "
                "(is_alive=False) — rebuilding"
            )
            try:
                stt_service.shutdown()
            except Exception:
                logger.exception("[ws_voice] old STT singleton shutdown failed")
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
        # Order matters: install hook FIRST so the ``ws_status`` event the
        # backend emits inside ``resume()`` (e.g. CONNECTING → CONNECTED)
        # arrives at the frontend chip. set_hook itself replays the
        # current state, so even before resume() flips anything the UI
        # gets one event (IDLE) to render off.
        stt_service.set_hook(on_stt_event)
        # Mic-driven lifecycle: open the upstream WS (Soniox) or unblock
        # the local worker. Without this the singleton stays IDLE and
        # silently drops audio. The route's ``finally`` block calls
        # ``pause()`` to mirror — singletons stay around between sessions
        # cheaply (no model reload on next mic-on) but the upstream WS
        # is only held while the user is actively on mic.
        #
        # Why to_thread: on cold-boot / rebuild paths the WS thread
        # hasn't attached its asyncio loop yet, so resume() spins for
        # up to ~500 ms waiting for ``_loop``. Running it on the event
        # loop directly would stall every other socket frame for that
        # window. The thread is cheap (one shot per session).
        try:
            await asyncio.to_thread(stt_service.resume)
        except Exception:
            logger.exception("[ws_voice] STT resume() failed")

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
        if provider is None:
            # Lifespan should have built this; surface a real error rather
            # than crashing on the next attribute access. Don't toggle
            # bot_speaking — there's nothing to speak.
            logger.error("[ws_voice] speak() with no chat TTS provider")
            await out_queue.put({"type": "error", "detail": "Chat TTS provider not initialised"})
            return
        stream_pcm = getattr(provider, "stream_pcm", None)
        path = "stream_pcm" if stream_pcm is not None else "stream_audio+ffmpeg"
        bot_speaking = True
        await out_queue.put({"type": "tts_start"})
        bytes_sent = 0
        chunks_sent = 0
        logger.info(
            "[ws_voice] speak() start: provider=%s path=%s text_len=%d",
            type(provider).__name__, path, len(text),
        )
        try:
            if stream_pcm is not None:
                async for pcm in stream_pcm(text):
                    bytes_sent += len(pcm)
                    chunks_sent += 1
                    await out_queue.put(pcm)
            else:
                async for pcm in _mp3_stream_to_pcm(provider.stream_audio(text)):
                    bytes_sent += len(pcm)
                    chunks_sent += 1
                    await out_queue.put(pcm)
            logger.info(
                "[ws_voice] speak() done: chunks=%d bytes=%d",
                chunks_sent, bytes_sent,
            )
        except asyncio.CancelledError:
            await out_queue.put({"type": "barge_in_ack"})
            raise
        finally:
            bot_speaking = False
            # Dump the mic PCM captured during this TTS turn to /data/voice_diag
            # so we can listen to what the server saw + compare RMS vs the
            # silent baseline. This is the ground truth for "is AEC working".
            try:
                if diag_enabled and diag_during_bot:
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

    # Whitelist of progress events worth surfacing to voice clients. The
    # SSE stream pushes many more (``thinking``, ``responding``, ``ping``…)
    # but the voice UI only wants tool-call lifecycle so it can render the
    # same compact "X tools used" bubble that text chat shows.
    _VOICE_PROGRESS_EVENTS = {"tool_request", "tool_done", "tool_running"}

    async def _drain_progress(req_id: str) -> None:
        """Forward selected progress events from ``progress_manager`` to the
        WS out queue so the dashboard's ``pushToolCall`` path lights up
        identically to the /chat-stream POST flow.

        Runs until cancelled by the surrounding turn handler — there is no
        terminal event here because we send ``assistant_message`` ourselves
        (the SSE-style ``done`` event isn't pushed for voice turns).
        """
        # ``progress_manager.get`` returns the Queue object (sync); await
        # ``.get()`` on that to pop events as they're pushed.
        q = progress_manager.get(req_id)
        if q is None:
            return
        try:
            while True:
                evt = await q.get()
                if evt.get("type") in _VOICE_PROGRESS_EVENTS:
                    await out_queue.put(evt)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("[ws_voice] progress drain failed (req=%s)", req_id)

    async def _handle_user_turn(text: str) -> None:
        """One turn of the conversation: echo user → invoke agent → speak.

        Event order chosen so the dashboard can render an "agent is
        thinking" placeholder synchronously with the user message:

            user_message → agent_thinking → (resume_and_send) →
            (tool_request / tool_done … per tool call) →
            session (if new) → assistant_message → tts_start → audio chunks
            → tts_end

        Cancellation policy: when ``recording_start`` fires while we're in
        flight, the outer scheduler cancels this task. CancelledError
        propagates up; the writer is already draining tts_interruption and
        the next user_turn task replaces this one. Whatever progress hooks
        we attached are restored in the ``finally`` block so a cancelled
        turn doesn't leak hooks into the next one.
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

            # Per-turn request id — same shape as /chat-stream. Lets the
            # progress queue be scoped to this turn (parallel turns on the
            # same socket would otherwise mix events).
            req_id = str(uuid.uuid4())
            progress_manager.create(req_id)
            progress_hooks = create_progress_hooks(req_id, session_id=session_id)

            # Tag every LLM call this turn makes with req_id so the
            # always-on token-persistence hook can correlate token_usage
            # rows back to this voice turn. ContextVar is asyncio-aware:
            # parallel turns on different sockets see their own values.
            from services.sse_progress import current_run_id
            _run_token = current_run_id.set(req_id)

            # Snapshot original hooks so we can restore even if the task is
            # cancelled mid-turn. We also remember the *exact* hook object
            # we attached so the restore step can detect when a newer
            # turn (started by a barge-in cancellation) has already swapped
            # in its own hook chain — restoring blindly in that case would
            # wipe the new turn's progress hooks and tool events from
            # the new turn would never reach the WS (the "tool bubble
            # disappears when user barges in" regression). The race is real
            # because ``task.cancel()`` only schedules CancelledError;
            # the next dispatched turn can attach its hooks before our
            # ``finally`` actually runs.
            original_hooks: dict[str, Any] = {}
            attached_hooks: dict[str, Any] = {}
            for ag_name, agent in state.agent_app._agents.items():
                original_hooks[ag_name] = getattr(agent, "tool_runner_hooks", None)
                existing = original_hooks[ag_name]
                merged = merge_hooks(existing, progress_hooks) if existing else progress_hooks
                attached_hooks[ag_name] = merged
                agent.tool_runner_hooks = merged

            drain_task = asyncio.create_task(_drain_progress(req_id))
            try:
                # Voice + chat both send the user text raw — voice formatting
                # comes from the chosen TTS provider (configurable in Settings).
                response, new_session_id = await state.session_service.resume_and_send(
                    state.agent_app,
                    text,
                    session_id,
                    agent_name=agent_name,
                )
            finally:
                # Restore hooks ONLY if our attached chain is still the
                # current one. If a newer turn beat us to swapping in its
                # own progress hooks, leave them alone — it owns the
                # chain now and our "original" snapshot is stale.
                for ag_name, agent in state.agent_app._agents.items():
                    if ag_name not in original_hooks:
                        continue
                    if getattr(agent, "tool_runner_hooks", None) is attached_hooks[ag_name]:
                        agent.tool_runner_hooks = original_hooks[ag_name]
                drain_task.cancel()
                try:
                    await drain_task
                except (asyncio.CancelledError, Exception):
                    pass
                progress_manager.remove(req_id)
                current_run_id.reset(_run_token)

            # Cancellation absorbed by fast-agent: the OpenAI provider
            # (and likely others) catches ``asyncio.CancelledError`` from
            # an interrupted LLM call and returns an empty Prompt with
            # stop_reason=CANCELLED rather than re-raising. That means
            # ``await resume_and_send`` *returns* ("", session_id)
            # instead of propagating the cancellation we issued. Without
            # the check below, the cancelled turn would proceed past
            # this point and emit ``assistant_message empty=True`` —
            # which the frontend's ``_dropPending`` path uses to remove
            # the in-flight placeholder. By that time ``pendingAgentMsgId``
            # already points to the *new* turn's placeholder (the
            # reason we cancelled was a fresh user_message starting a
            # new turn), so the cleanup code rips out the new turn's
            # bubble + tool bar. This was the umbrella cause for the
            # "placeholder gone / tool bubble gone / TTS silent" trio.
            #
            # ``Task.cancelling()`` returns >0 if cancel() was ever
            # called on this task, even when the inner await absorbed
            # the exception. Python 3.11+; we're on 3.13.
            try:
                cur = asyncio.current_task()
                if cur is not None and cur.cancelling() > 0:
                    logger.info(
                        "[ws_voice] turn req=%s cancelled mid-LLM "
                        "— silent exit so the new turn's events stand",
                        req_id,
                    )
                    return
            except Exception:
                pass

            if new_session_id and new_session_id != session_id:
                session_id = new_session_id
                await out_queue.put({"type": "session", "id": session_id})

            # Mirror the chat-stream POST flow: persist the agent's full
            # message_history snapshot to SQLite so the Agents tab's
            # Context Window panel actually shows voice turns. Without
            # this, voice conversations were invisible to the
            # context-history view ("context window was not being saved
            # to db after agent idle" bug). Best-effort — never
            # break the speak() path on a save failure.
            try:
                from services.context_persistence import save_agent_context
                target_name = agent_name or "Jarvis"
                target_agent_obj = state.agent_app._agents.get(target_name)
                if target_agent_obj:
                    await save_agent_context(
                        target_agent_obj,
                        req_id,
                        trigger="voice_turn_complete",
                        agent_name=target_name,
                        session_id=session_id,
                    )
            except Exception:
                logger.warning("[CONTEXT] voice turn save failed", exc_info=True)

            response = (response or "").strip()
            logger.info(
                "[ws_voice] turn done req=%s response_len=%d empty=%s",
                req_id, len(response), not response,
            )
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
            # Cancelled before / during agent generation. Do NOT emit
            # another tts_interruption here — ``_cancel_inflight`` already
            # pushed one when it called ``.cancel()`` on us. Emitting a
            # second one races against the *new* turn's ``agent_thinking``
            # event: if it lands after the new placeholder is created, the
            # frontend's ``_dropPending`` yanks the fresh placeholder out
            # (the "when user barges in to correct, the placeholder disappears" bug).
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
                    if diag_enabled and len(diag_during_bot) < _DIAG_BUFFER_MAX_BYTES:
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
                elif kind == "start":
                    # Accepting the socket already starts the STT pipeline; the
                    # client sends ``start`` mostly as a handshake-ack. The
                    # one piece of payload we honour is ``mode: "dictation"``
                    # which gates LLM/TTS dispatch on ``final_transcript``.
                    if payload.get("mode") == "dictation":
                        dictation_mode = True
                        logger.info("[ws_voice] dictation mode enabled for this session")
    except WebSocketDisconnect:
        pass
    finally:
        if stt_service is not None:
            # Mirror of the resume() call above: pause closes the upstream
            # WS (Soniox) or stops audio gating into the local worker,
            # but keeps the singleton + thread alive for the next mic-on.
            # Best-effort cleanup — the client is disconnecting anyway, so
            # any in-flight IDLE ``ws_status`` event the hook would have
            # surfaced is moot. pause() is fire-and-forget cross-thread
            # (the real close lands later when ``_run_ws`` notices the
            # cleared active flag), and set_hook(None) detaches before
            # that lands; this is intentional, not an ordering bug.
            try:
                stt_service.pause()
            except Exception:
                logger.exception("[ws_voice] STT pause() failed")
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
