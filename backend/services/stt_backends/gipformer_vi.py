"""Gipformer-65M Vietnamese ASR backend (sherpa-onnx).

Vietnamese-only, MIT-licensed, ~73 MB int8 ONNX. Lazy-downloads model
files from HuggingFace on first build (cached under ~/.cache/huggingface).

Pipeline mirrors faster-whisper's segment-then-transcribe model so that
``ws_voice`` doesn't have to special-case backends:

    feed_audio(pcm)  ──→  worker thread:
        np.float32 ─→ silero VAD ─→ on speech-end pop SpeechSegment ─→
        sherpa-onnx OfflineRecognizer ─→ emit "final_transcript"

Events emitted (match RealtimeSTTService so ws_voice glue is identical):
    vad_start / vad_stop          — speech edges
    recording_start / recording_stop — barge-in trigger
    final_transcript              — full utterance text
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
FEATURE_DIM = 80
HF_REPO = "g-group-ai-lab/gipformer-65M-rnnt"
HF_FILE_BASENAME = "epoch-35-avg-6"

# Silero VAD v4.0 ONNX — cached under XDG cache. Pinned to v4 because
# sherpa-onnx 1.10's bundled onnxruntime cannot load IR v10 models
# (the newer Silero v5 / onnx-community variants).
SILERO_VAD_URL = (
    "https://github.com/snakers4/silero-vad/raw/v4.0/files/silero_vad.onnx"
)


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    d = Path(base) / "jarvis" / "gipformer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_silero_vad() -> str:
    """Download Silero VAD v4 ONNX once; return cached path."""
    target = _cache_dir() / "silero_vad_v4.onnx"
    if not target.exists():
        logger.info("[Gipformer] Downloading Silero VAD ONNX → %s", target)
        urllib.request.urlretrieve(SILERO_VAD_URL, target)
    return str(target)


def _download_gipformer(quantize: str) -> dict[str, str]:
    """Lazy-fetch encoder/decoder/joiner ONNX + tokens from HuggingFace."""
    from huggingface_hub import hf_hub_download

    suffix = ".int8.onnx" if quantize == "int8" else ".onnx"
    files = {
        "encoder": f"encoder-{HF_FILE_BASENAME}{suffix}",
        "decoder": f"decoder-{HF_FILE_BASENAME}{suffix}",
        "joiner":  f"joiner-{HF_FILE_BASENAME}{suffix}",
        "tokens":  "tokens.txt",
    }
    paths: dict[str, str] = {}
    for k, name in files.items():
        paths[k] = hf_hub_download(repo_id=HF_REPO, filename=name)
    return paths


class GipformerSTTService:
    """sherpa-onnx Gipformer + Silero VAD; duck-types ``RealtimeSTTService``.

    The interface ``ws_voice`` cares about:
    ``feed_audio`` / ``set_hook`` / ``start_listen_loop`` / ``shutdown``
    plus the ``on_*`` callback methods that fan out to the registered hook.
    """

    def __init__(self, recognizer, vad) -> None:
        self._recognizer = recognizer
        self._vad = vad
        self._hook: Optional[Callable[[str, dict[str, Any]], None]] = None
        self._lock = threading.Lock()
        self._closed = False
        self._chunks: queue.Queue[bytes] = queue.Queue()
        self._listen_thread: Optional[threading.Thread] = None
        self._was_speaking = False

    # ---- hook glue (mirrors RealtimeSTTService) ---------------------------

    def set_hook(self, hook):
        with self._lock:
            self._hook = hook

    def _emit(self, event: str, payload: dict):
        with self._lock:
            hook = self._hook
        if hook is None:
            return
        try:
            hook(event, payload)
        except Exception:
            logger.exception("[Gipformer] hook raised on event %s", event)

    # Only the events the worker actually emits. RealtimeSTT exposes
    # partial/stable/wake-word too, but sherpa-onnx OfflineRecognizer is
    # one-shot (no streaming intermediates) and the registry surfaces
    # only ``wake_word: off`` here — adding stubs for events that never
    # fire would just be dead code.
    def on_final(self, text: str) -> None:
        logger.info("[Gipformer] final: %r", text[:80])
        self._emit("final_transcript", {"text": text})

    def on_vad_start(self) -> None:
        self._emit("vad_start", {})

    def on_vad_stop(self) -> None:
        self._emit("vad_stop", {})

    def on_recording_start(self) -> None:
        self._emit("recording_start", {})

    def on_recording_stop(self) -> None:
        self._emit("recording_stop", {})

    # ---- audio path ------------------------------------------------------

    def feed_audio(self, pcm_chunk: bytes) -> None:
        if self._closed:
            return
        self._chunks.put(pcm_chunk)

    def start_listen_loop(self) -> None:
        if self._listen_thread and self._listen_thread.is_alive():
            return
        t = threading.Thread(
            target=self._loop, name="gipformer-listen-loop", daemon=True
        )
        self._listen_thread = t
        t.start()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._listen_thread is not None:
            self._listen_thread.join(timeout=2.0)

    # ---- worker -----------------------------------------------------------

    def _loop(self) -> None:
        import numpy as np

        while not self._closed:
            try:
                chunk = self._chunks.get(timeout=0.5)
            except queue.Empty:
                continue
            samples = (
                np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
            )
            try:
                self._vad.accept_waveform(samples)
            except Exception:
                logger.exception("[Gipformer] VAD accept_waveform failed")
                continue

            now_speaking = self._vad.is_speech_detected()
            if now_speaking and not self._was_speaking:
                self.on_vad_start()
                self.on_recording_start()
            elif not now_speaking and self._was_speaking:
                self.on_vad_stop()
                self.on_recording_stop()
            self._was_speaking = now_speaking

            while not self._vad.empty():
                # Order matters: ``front`` returns a reference into the VAD's
                # internal buffer; ``pop()`` deallocates that slot, so any
                # later read of ``seg.samples`` would observe garbage (it
                # surfaced as Conv encoder rejecting input shape {0,80}
                # because the dangling samples appeared empty). Hand the
                # samples to the recognizer's stream first — that copies
                # them — then pop.
                stream = self._recognizer.create_stream()
                stream.accept_waveform(SAMPLE_RATE, self._vad.front.samples)
                self._vad.pop()
                self._decode(stream)

    def _decode(self, stream) -> None:
        try:
            self._recognizer.decode_streams([stream])
            text = stream.result.text.strip()
        except Exception:
            logger.exception("[Gipformer] decode failed")
            return
        if text:
            self.on_final(text)


def build(config: dict[str, Any]) -> GipformerSTTService:
    """Construct a Gipformer-65M backed STT service.

    Wake-word backends other than ``off`` are not supported here — the
    upstream loop drives transcription directly off Silero VAD. The
    registry surfaces only the ``off`` option so the UI never offers a
    Porcupine/OWW config that would silently be ignored.
    """
    import sherpa_onnx

    params = dict(config.get("params") or {})
    quantize = params.get("quantize", "int8")
    decoding_method = params.get("decoding_method", "modified_beam_search")
    num_threads = int(params.get("num_threads", 4))
    silence_s = float(params.get("post_speech_silence_duration", 0.7))
    vad_threshold = float(params.get("silero_sensitivity", 0.5))
    min_speech_s = float(params.get("min_speech_duration", 0.25))

    paths = _download_gipformer(quantize)
    recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=paths["encoder"],
        decoder=paths["decoder"],
        joiner=paths["joiner"],
        tokens=paths["tokens"],
        num_threads=num_threads,
        sample_rate=SAMPLE_RATE,
        feature_dim=FEATURE_DIM,
        decoding_method=decoding_method,
    )

    vad_cfg = sherpa_onnx.VadModelConfig()
    vad_cfg.silero_vad.model = _ensure_silero_vad()
    vad_cfg.silero_vad.threshold = vad_threshold
    vad_cfg.silero_vad.min_silence_duration = silence_s
    vad_cfg.silero_vad.min_speech_duration = min_speech_s
    vad_cfg.sample_rate = SAMPLE_RATE
    vad = sherpa_onnx.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=60)

    svc = GipformerSTTService(recognizer, vad)
    svc.start_listen_loop()
    return svc
