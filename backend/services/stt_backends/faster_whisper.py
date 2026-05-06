"""faster-whisper backend (default) — built on RealtimeSTT.AudioToTextRecorder.

Multilingual (vi+en code-switch via ``language=auto``). Heavy: pulls torch
+ whisper weights on first build; the listen loop spawns a daemon thread
to drive VAD-based recording cycles.
"""
from __future__ import annotations

from typing import Any

from services.stt_realtime import (
    RealtimeSTTService,
    _patch_torch_hub_trust,
)


def build(config: dict[str, Any]) -> RealtimeSTTService:
    """Construct a faster-whisper-backed STT service."""
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
