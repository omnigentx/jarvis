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
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


EventHook = Callable[[str, dict[str, Any]], None]


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
    """

    def __init__(self, recorder, *, language: str = "auto") -> None:
        self._recorder = recorder
        self._hook: Optional[EventHook] = None
        self._lock = threading.Lock()
        self._language = language
        self._closed = False
        self._listen_thread: Optional[threading.Thread] = None

    # ---- hook management --------------------------------------------------

    def set_hook(self, hook: Optional[EventHook]) -> None:
        """Register the active event consumer. ``None`` detaches."""
        with self._lock:
            self._hook = hook

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
        if self._closed:
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

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
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


def build_stt_service(config: dict[str, Any]) -> RealtimeSTTService:
    """Construct the STT service from a registry config dict.

    Heavy imports happen here, not at module load.
    """
    _patch_torch_hub_trust()
    from RealtimeSTT import AudioToTextRecorder

    params = dict(config.get("params") or {})
    # Map "auto" → None so faster-whisper auto-detects language per chunk.
    lang = params.pop("language", "auto")
    if lang == "auto":
        lang = None

    wake = config.get("wake_word") or {}
    wake_backend = wake.get("backend", "off")
    wake_params = wake.get("params") or {}

    kwargs: dict[str, Any] = {
        "use_microphone": False,  # WS-fed
        "spinner": False,
        "language": lang,
        **{k: v for k, v in params.items() if v not in (None, "")},
    }

    if wake_backend != "off":
        kwargs.update({
            "wakeword_backend": "pvporcupine" if wake_backend == "porcupine" else "oww",
            **{k: v for k, v in wake_params.items() if v not in (None, "")},
        })

    # Service is built first so callback closures can reference it before
    # the recorder construction (which may lazy-load models).
    holder: dict[str, RealtimeSTTService] = {}

    def _wrap(meth_name: str):
        def fn(*args, **kwargs):
            svc = holder.get("svc")
            if svc is None:
                return
            getattr(svc, meth_name)(*args, **kwargs)
        return fn

    kwargs.setdefault("on_realtime_transcription_update", _wrap("on_partial"))
    kwargs.setdefault("on_realtime_transcription_stabilized", _wrap("on_stable"))
    kwargs.setdefault("on_vad_detect_start", _wrap("on_vad_start"))
    kwargs.setdefault("on_vad_detect_stop", _wrap("on_vad_stop"))
    # on_recording_start/stop are RealtimeSTT's "VAD locked-on" signals —
    # we use recording_start as the canonical barge-in trigger (matches
    # RealtimeVoiceChat's design).
    kwargs.setdefault("on_recording_start", _wrap("on_recording_start"))
    kwargs.setdefault("on_recording_stop", _wrap("on_recording_stop"))
    if wake_backend != "off":
        kwargs.setdefault("on_wakeword_detected", _wrap("on_wake_word"))

    recorder = AudioToTextRecorder(**kwargs)
    svc = RealtimeSTTService(recorder, language=lang or "auto")
    holder["svc"] = svc
    # Activate the VAD-driven recording cycle. Without this loop the
    # recorder is in a passive state — audio fed via feed_audio fills the
    # buffer but nothing ever transcribes (no callbacks fire).
    svc.start_listen_loop()
    return svc
