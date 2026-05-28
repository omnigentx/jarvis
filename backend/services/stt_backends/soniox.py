"""Soniox real-time STT backend.

Streams 16 kHz int16 PCM (fed via ``feed_audio``) to
``wss://stt-rt.soniox.com/transcribe-websocket`` and forwards transcripts
through the same hook surface as :class:`services.stt_realtime.RealtimeSTTService`.

The thread/queue/reconnect/hook scaffolding lives in
:mod:`services.stt_backends._ws_streaming`. This module only encodes the
Soniox-specific protocol bits (config-message shape, ``<end>`` endpoint
token, error format) so adding the next cloud STT (Deepgram, AssemblyAI,
Google) is a sibling file with the same shape.

Endpoint detection: with ``enable_endpoint_detection=true`` Soniox emits a
special ``{"text": "<end>", "is_final": true}`` token whenever the model
decides the speaker stopped. We flush the accumulated finalized tokens as
the next ``final_transcript`` and reset.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar

from services.stt_backends._ws_streaming import WSStreamingSTT

logger = logging.getLogger(__name__)

SONIOX_STT_WS_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
ENDPOINT_TOKEN_TEXT = "<end>"


class SonioxSTTService(WSStreamingSTT):
    """Soniox-flavoured cloud STT over the shared WS streaming base."""

    WS_URL: ClassVar[str] = SONIOX_STT_WS_URL
    LOG_TAG: ClassVar[str] = "Soniox STT"

    def __init__(self, *, api_key: str, params: dict[str, Any]) -> None:
        if not api_key:
            raise ValueError("Soniox STT requires an API key")
        super().__init__(sample_rate=int(params.get("sample_rate") or 16000))

        self._api_key = api_key
        self._model = params.get("model") or "stt-rt-v4"
        self._language_hints = _parse_language_hints(params.get("language_hints"))
        self._enable_language_id = bool(params.get("enable_language_identification"))
        self._enable_diarization = bool(params.get("enable_speaker_diarization"))

        # Per-utterance buffer of finalized tokens. Reset on every endpoint.
        self._final_buffer: list[str] = []
        self._provisional_tail: str = ""

    # ---- WSStreamingSTT extension points ---------------------------------

    def _build_config_message(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "api_key": self._api_key,
            "model": self._model,
            "audio_format": "pcm_s16le",
            "sample_rate": self._sample_rate,
            "num_channels": 1,
            "enable_endpoint_detection": True,
        }
        if self._language_hints:
            cfg["language_hints"] = self._language_hints
        if self._enable_language_id:
            cfg["enable_language_identification"] = True
        if self._enable_diarization:
            cfg["enable_speaker_diarization"] = True
        return cfg

    def _on_connected(self) -> None:
        # Clean slate on (re)connect so leftovers from a dropped session
        # don't bleed into the new transcript.
        self._final_buffer.clear()
        self._provisional_tail = ""

    def _handle_event(self, data: dict[str, Any]) -> None:
        err_code = data.get("error_code")
        if err_code:
            logger.error("[Soniox STT] error %s: %s", err_code, data.get("error_message"))
            self._emit("error", {
                "detail": data.get("error_message") or f"soniox error {err_code}",
            })
            return

        for tk in data.get("tokens") or []:
            self._handle_token(tk)

        if data.get("finished"):
            # ``async for msg in ws`` will exit on its own once the server
            # closes the socket; no explicit return-from-receiver needed.
            return

    def _handle_token(self, tk: dict[str, Any]) -> None:
        text = tk.get("text") or ""
        is_final = bool(tk.get("is_final"))

        if text == ENDPOINT_TOKEN_TEXT:
            final = "".join(self._final_buffer).strip()
            self._final_buffer.clear()
            self._provisional_tail = ""
            self._emit_final(final)
            self._emit_endpoint()
            return

        if is_final:
            self._final_buffer.append(text)
            self._provisional_tail = ""
        else:
            # Soniox emits provisional tokens that may be revised; track only
            # the latest non-final text so the running partial doesn't snowball.
            self._provisional_tail = text

        snapshot = "".join(self._final_buffer) + self._provisional_tail
        self._emit_partial(snapshot)


def _parse_language_hints(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(s).strip() for s in raw if str(s).strip()]
    return []


def build(config: dict[str, Any]) -> SonioxSTTService:
    """Factory used by :mod:`services.stt_realtime._BACKEND_FACTORIES`.

    The API key is loaded from the same encrypted ``voice.secrets.soniox.api_key``
    slot that the TTS engine uses — Soniox issues one key per account that
    works against both STT and TTS endpoints, so duplicating slots would
    only invite drift.
    """
    from services.config_service import config_service as _cs

    api_key = _cs.get("voice", "secrets.soniox.api_key")
    if not api_key:
        raise RuntimeError(
            "Soniox STT selected but no API key configured. "
            "Set it under Settings → Voice → Soniox."
        )

    params = dict(config.get("params") or {})
    svc = SonioxSTTService(api_key=api_key, params=params)
    svc.start_listen_loop()
    return svc
