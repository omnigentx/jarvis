"""RealtimeSTT adapter — wraps AudioToTextRecorder for WebSocket-fed audio.

Design goals:
* The browser captures mic via getUserMedia + AudioWorklet, downsamples to
  16 kHz mono int16, and ships PCM frames over /ws/voice/in. Server feeds
  those bytes into ``AudioToTextRecorder.feed_audio()``. No mic on the
  server side — fully decoupled transport.
* Callbacks (partial transcript, VAD start/stop, wake-word) are forwarded
  to a registered hook so /ws/voice/out can fan them out as JSON events.
* Heavy imports (torch, faster-whisper) live behind ``build_stt_service`` so
  module-import overhead stays low for tests / non-voice routes.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from services.stt_backends.types import (
    EventHook,
    STTConnectionState,
)

logger = logging.getLogger(__name__)


class RealtimeSTTService:
    """Thin async-friendly wrapper around RealtimeSTT.AudioToTextRecorder.

    Single recorder per process — RealtimeSTT spawns multiprocessing workers
    so duplicating instances would multiply GPU/CPU load. The active hook
    can be swapped at runtime so /ws/voice connections take exclusive turns.

    A dedicated listen-loop thread calls ``recorder.text()`` continuously.
    Why: RealtimeSTT only **enables** voice-activity-driven recording inside
    ``wait_audio()`` (called from ``text()``); without that call VAD
    callbacks never fire even when audio is being fed. The thread blocks on
    each ``text()`` call until VAD detects a speech-end transition, then
    emits the final transcript via ``on_final`` and loops back.

    Implements :class:`services.stt_backends.types.STTServiceProtocol`. As a
    local backend the model is always loaded and there is no upstream WS to
    drop, so ``resume`` / ``pause`` flip an in-process gating flag that the
    audio path checks — they only emit ``ws_status`` events for UI
    consistency with cloud providers, not real connection lifecycle.
    """

    def __init__(self, recorder, *, language: str = "auto") -> None:
        self._recorder = recorder
        self._hook: Optional[EventHook] = None
        self._lock = threading.Lock()
        self._language = language
        self._closed = False
        self._listen_thread: Optional[threading.Thread] = None
        # ``_active`` is False until ``resume()`` flips it. Symmetric with
        # cloud backends so the route's mic-driven lifecycle works the
        # same for both: mic on → resume() → audio flows; mic off →
        # pause() → audio drops at feed_audio without tearing down the
        # listen thread (model stays warm for the next session).
        self._active = False
        self._connection_state: STTConnectionState = STTConnectionState.IDLE

    # ---- hook management --------------------------------------------------

    def set_hook(self, hook: Optional[EventHook]) -> None:
        """Register the active event consumer. ``None`` detaches.

        Replays current ``connection_state`` to a fresh subscriber so a
        late-attached frontend sees the right chip immediately.
        """
        with self._lock:
            self._hook = hook
        if hook is not None:
            self._emit("ws_status", {
                "state": self._connection_state.value,
                "attempt": 0,
                "detail": "hook attached — replay current state",
            })

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        with self._lock:
            hook = self._hook
        if hook is None:
            return
        try:
            hook(event, payload)
        except Exception:
            logger.exception("[STT] hook %s raised on event %s", hook, event)

    # ---- audio path -------------------------------------------------------

    def feed_audio(self, pcm_chunk: bytes) -> None:
        """Push 16 kHz mono int16 PCM bytes from the WebSocket into the recorder."""
        if self._closed or not self._active:
            return
        # DEBUG-level amplitude meter so we can diagnose silent-mic issues
        # without spamming production logs. Re-enable by setting
        # services.stt_realtime to DEBUG.
        if logger.isEnabledFor(logging.DEBUG):
            self._fed_chunks = getattr(self, "_fed_chunks", 0) + 1
            self._fed_bytes = getattr(self, "_fed_bytes", 0) + len(pcm_chunk)
            if self._fed_chunks % 50 == 1:
                try:
                    import struct
                    samples = struct.unpack(f"<{len(pcm_chunk)//2}h", pcm_chunk)
                    if samples:
                        sq_sum = sum(s * s for s in samples) / len(samples)
                        rms = int(sq_sum ** 0.5)
                        peak = max(abs(s) for s in samples)
                    else:
                        rms = peak = 0
                except Exception:
                    rms = peak = -1
                logger.debug(
                    "[STT] feed_audio: chunk_no=%d bytes=%d total=%.1fs rms=%d peak=%d",
                    self._fed_chunks, len(pcm_chunk),
                    self._fed_bytes / (16000 * 2), rms, peak,
                )
        self._recorder.feed_audio(pcm_chunk)

    def resume(self) -> None:
        """Activate the local backend. Drives IDLE → CONNECTED.

        No real connection to open — the model is already loaded — so this
        is effectively just flipping ``_active`` so ``feed_audio`` stops
        dropping. Idempotent.
        """
        if self._closed:
            logger.warning("[STT] resume() called after shutdown — ignored")
            return
        if self._active and self._connection_state == STTConnectionState.CONNECTED:
            return
        self._active = True
        self._set_state(STTConnectionState.CONNECTED, detail="local backend ready")

    def pause(self) -> None:
        """Deactivate the local backend. Drives → IDLE.

        ``feed_audio`` becomes a drop. The listen thread keeps running so
        the model stays warm for the next ``resume`` (cheap restart).
        Idempotent.
        """
        if not self._active and self._connection_state == STTConnectionState.IDLE:
            return
        self._active = False
        self._set_state(STTConnectionState.IDLE, detail="paused")

    @property
    def is_alive(self) -> bool:
        """True while listen thread is alive and the service hasn't been
        shut down. Distinct from ``_active`` — paused-but-warm is alive.
        """
        if self._closed:
            return False
        if self._listen_thread is None:
            # ``start_listen_loop`` not called yet — boot pending.
            return True
        return self._listen_thread.is_alive()

    @property
    def connection_state(self) -> STTConnectionState:
        return self._connection_state

    def _set_state(self, state: STTConnectionState, *, detail: str = "") -> None:
        """Transition + emit ``ws_status``. Only emits on actual change."""
        if state == self._connection_state and detail == "":
            return
        self._connection_state = state
        self._emit("ws_status", {
            "state": state.value,
            "attempt": 0,
            "detail": detail,
        })

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._active = False
        # Trip the recorder's interrupt so any pending ``text()`` call in the
        # listen loop returns instead of blocking on VAD forever.
        try:
            if hasattr(self._recorder, "interrupt_stop_event"):
                self._recorder.interrupt_stop_event.set()
        except Exception:
            pass
        try:
            self._recorder.shutdown()
        except Exception:
            logger.exception("[STT] recorder.shutdown failed")
        if self._listen_thread is not None:
            self._listen_thread.join(timeout=2.0)
        self._set_state(STTConnectionState.IDLE, detail="shutdown")

    def start_listen_loop(self) -> None:
        """Run ``recorder.text()`` in a daemon thread on a never-ending loop.

        Without this, ``wait_audio()`` is never called and VAD detection
        stays disabled — feed_audio fills the buffer but nothing transcribes.
        Each cycle: wait for VAD start → record → wait for VAD stop →
        transcribe → emit final → loop.
        """
        if self._listen_thread is not None and self._listen_thread.is_alive():
            return

        def _loop() -> None:
            import time as _time
            while not self._closed:
                try:
                    text = self._recorder.text()
                except Exception:
                    if self._closed:
                        return
                    logger.exception("[STT] listen loop error")
                    _time.sleep(0.3)
                    continue
                if text and text.strip():
                    self.on_final(text)

        t = threading.Thread(target=_loop, name="stt-listen-loop", daemon=True)
        self._listen_thread = t
        t.start()

    # ---- callbacks bridged from RealtimeSTT -------------------------------

    def on_partial(self, text: str) -> None:
        logger.debug("[STT] partial: %r", text[:80] if text else "")
        self._emit("partial_transcript", {"text": text})

    def on_stable(self, text: str) -> None:
        logger.debug("[STT] stable: %r", text[:80] if text else "")
        self._emit("stable_transcript", {"text": text})

    def on_final(self, text: str) -> None:
        logger.info("[STT] final: %r", text[:80] if text else "")
        self._emit("final_transcript", {"text": text})

    def on_vad_start(self) -> None:
        logger.debug("[STT] vad_start")
        self._emit("vad_start", {})

    def on_vad_stop(self) -> None:
        logger.debug("[STT] vad_stop")
        self._emit("vad_stop", {})

    def on_recording_start(self) -> None:
        # Fires when VAD locks onto the speaker — earlier than vad_stop, which
        # is when we know "they're really talking". RealtimeVoiceChat uses
        # this exact callback for barge-in: if the bot is mid-reply, this
        # signal cancels the ongoing TTS + LLM generation.
        logger.info("[STT] recording_start")
        self._emit("recording_start", {})

    def on_recording_stop(self) -> None:
        logger.debug("[STT] recording_stop")
        self._emit("recording_stop", {})

    def on_wake_word(self) -> None:
        logger.info("[STT] wake_word")
        self._emit("wake_word", {})


def _patch_torch_hub_trust() -> None:
    """Make torch.hub.load default to trust_repo=True.

    RealtimeSTT calls torch.hub.load("snakers4/silero-vad") inside
    AudioToTextRecorder.__init__ to load the Silero VAD model. With
    trust_repo unset, torch prompts via input() — under uvicorn there is no
    TTY, so the prompt raises EOFError and STT init bombs.

    We trust this repo unconditionally: it's the de-facto Silero VAD source
    and RealtimeSTT hardcodes it. Patching globally (idempotent — only
    wraps once) is smaller blast radius than forking RealtimeSTT for one
    keyword arg, and the wrapper only changes a default — explicit
    trust_repo args from any caller still win.
    """
    import torch.hub
    if getattr(torch.hub.load, "_jarvis_trusted", False):
        return
    _orig = torch.hub.load

    def _trusted_load(*args, **kwargs):
        kwargs.setdefault("trust_repo", True)
        return _orig(*args, **kwargs)

    _trusted_load._jarvis_trusted = True  # type: ignore[attr-defined]
    torch.hub.load = _trusted_load


# Dispatch table: backend id (matching ``STT_BACKENDS`` keys in the
# registry) → "module:function" string for the per-backend factory.
# Each factory takes the config dict and returns an object that
# duck-types ``RealtimeSTTService`` (feed_audio + set_hook +
# start_listen_loop + shutdown + the on_* callbacks).
#
# Adding a new backend = add 1 entry here + 1 module under
# services/stt_backends/. Nothing else in this file needs to change.
_BACKEND_FACTORIES: dict[str, str] = {
    "faster_whisper": "services.stt_backends.faster_whisper:build",
    "gipformer_vi":   "services.stt_backends.gipformer_vi:build",
    "soniox":         "services.stt_backends.soniox:build",
}


def build_stt_service(config: dict[str, Any]):
    """Dispatch to the per-backend factory selected by ``config["backend"]``.

    Heavy imports happen inside each backend module, not here, so loading
    this module stays cheap for tests and non-voice routes.

    The feature-flag check in ``stt_backends.assert_backend_enabled``
    runs FIRST — selecting a disabled backend (per ``STT_BACKENDS_ENABLED``
    env var) raises ``RuntimeError`` with the env-var name in the
    message so the operator can fix config without spelunking.
    """
    from services.stt_backends import assert_backend_enabled

    backend = config.get("backend", "faster_whisper")
    assert_backend_enabled(backend)  # raises on unknown or disabled

    spec = _BACKEND_FACTORIES.get(backend)
    if spec is None:
        # Should be unreachable — assert_backend_enabled would have raised
        # ValueError above. Kept as belt-and-braces in case _KNOWN and
        # _BACKEND_FACTORIES drift.
        raise ValueError(
            f"Backend {backend!r} passed allowlist but has no factory entry. "
            f"_BACKEND_FACTORIES out of sync with stt_backends._KNOWN."
        )
    mod_path, fn_name = spec.split(":")
    module = __import__(mod_path, fromlist=[fn_name])
    return getattr(module, fn_name)(config)
