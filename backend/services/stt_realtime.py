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
    """

    def __init__(self, recorder, *, language: str = "auto") -> None:
        self._recorder = recorder
        self._hook: Optional[EventHook] = None
        self._lock = threading.Lock()
        self._language = language
        self._closed = False

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
        self._recorder.feed_audio(pcm_chunk)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._recorder.shutdown()
        except Exception:
            logger.exception("[STT] recorder.shutdown failed")

    # ---- callbacks bridged from RealtimeSTT -------------------------------

    def on_partial(self, text: str) -> None:
        self._emit("partial_transcript", {"text": text})

    def on_stable(self, text: str) -> None:
        self._emit("stable_transcript", {"text": text})

    def on_final(self, text: str) -> None:
        self._emit("final_transcript", {"text": text})

    def on_vad_start(self) -> None:
        self._emit("vad_start", {})

    def on_vad_stop(self) -> None:
        self._emit("vad_stop", {})

    def on_wake_word(self) -> None:
        self._emit("wake_word", {})


def build_stt_service(config: dict[str, Any]) -> RealtimeSTTService:
    """Construct the STT service from a registry config dict.

    Heavy imports happen here, not at module load.
    """
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
    if wake_backend != "off":
        kwargs.setdefault("on_wakeword_detected", _wrap("on_wake_word"))

    recorder = AudioToTextRecorder(**kwargs)
    svc = RealtimeSTTService(recorder, language=lang or "auto")
    holder["svc"] = svc
    return svc
