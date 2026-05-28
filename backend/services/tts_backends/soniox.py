"""Soniox real-time TTS provider.

Implements :class:`services.tts.TTSProvider` over Soniox' bidirectional
WebSocket at ``wss://tts-rt.soniox.com/tts-websocket``. The provider opens
one socket per request, streams a single text payload, and yields decoded
audio chunks as the server returns them.

Why not RealtimeTTS: the upstream library has no Soniox engine class, so
plugging Soniox in via the registry's ``realtimetts_engine`` field would
require maintaining a fork. A direct WS client is ~150 lines and matches
the shape of EdgeTTSProvider (the other engine that already bypasses
RealtimeTTS).

Two stream methods:
* ``stream_audio`` — yields MP3 bytes, used by the legacy MP3 route
  (/api/tts/{request_id}) and the preview endpoint.
* ``stream_pcm`` — yields raw int16 mono PCM, used by /ws/voice/out so the
  browser can play seamlessly through the AudioWorklet (no MP3 frame
  boundary glitches).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any, AsyncIterator, Optional

from services.tts import TTSProvider

logger = logging.getLogger(__name__)

SONIOX_TTS_WS_URL = "wss://tts-rt.soniox.com/tts-websocket"
DEFAULT_MODEL = "tts-rt-v1"
DEFAULT_VOICE = "Adrian"
DEFAULT_LANGUAGE = "en"
DEFAULT_SAMPLE_RATE = 24000


class SonioxTTSProvider(TTSProvider):
    """Soniox real-time TTS over WebSocket."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        language: str = DEFAULT_LANGUAGE,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        if not api_key:
            raise ValueError("Soniox TTS requires an API key")
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._language = language
        self._sample_rate = int(sample_rate)

    # ---- TTSProvider API --------------------------------------------------

    async def generate_audio(self, text: str) -> Optional[bytes]:
        if not text or not text.strip():
            return None
        chunks: list[bytes] = []
        async for chunk in self.stream_audio(text):
            chunks.append(chunk)
        return b"".join(chunks) if chunks else None

    async def stream_audio(self, text: str) -> AsyncIterator[bytes]:
        """Yield MP3 audio bytes as Soniox produces them."""
        async for chunk in self._stream(text, audio_format="mp3"):
            yield chunk

    async def stream_pcm(self, text: str) -> AsyncIterator[bytes]:
        """Yield raw int16 mono PCM for the low-latency WS path."""
        async for chunk in self._stream(text, audio_format="pcm_s16le"):
            yield chunk

    # ---- internals --------------------------------------------------------

    async def _stream(self, text: str, *, audio_format: str) -> AsyncIterator[bytes]:
        if not text or not text.strip():
            return

        try:
            import websockets
        except ImportError as exc:  # pragma: no cover — present via uv.lock
            logger.error("[Soniox TTS] websockets package not available: %s", exc)
            raise

        stream_id = f"jarvis-{uuid.uuid4().hex[:12]}"
        config = {
            "api_key": self._api_key,
            "stream_id": stream_id,
            "model": self._model,
            "voice": self._voice,
            "language": self._language,
            "audio_format": audio_format,
            "sample_rate": self._sample_rate,
        }

        async with websockets.connect(SONIOX_TTS_WS_URL, max_size=None) as ws:
            await ws.send(json.dumps(config))
            # Send the entire text in one chunk with text_end=True. Soniox
            # supports incremental text but our callers pass a single
            # already-buffered string, so chunking buys nothing here.
            await ws.send(json.dumps({
                "text": text,
                "text_end": True,
                "stream_id": stream_id,
            }))

            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    # Soniox returns JSON-only for TTS; surface binary as a
                    # protocol surprise rather than silently swallowing it.
                    logger.warning("[Soniox TTS] unexpected binary frame")
                    continue
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    logger.debug("[Soniox TTS] non-JSON message ignored")
                    continue

                err_code = data.get("error_code")
                if err_code:
                    raise RuntimeError(
                        f"Soniox TTS error {err_code}: "
                        f"{data.get('error_message') or 'unknown'}"
                    )

                if data.get("terminated"):
                    return

                b64 = data.get("audio")
                if b64:
                    try:
                        yield base64.b64decode(b64)
                    except (ValueError, TypeError) as exc:
                        logger.warning("[Soniox TTS] base64 decode failed: %s", exc)
                        continue

                if data.get("audio_end"):
                    # Wait for the server's explicit ``terminated`` to close
                    # cleanly; otherwise we may miss trailing audio frames if
                    # a chunk crosses the audio_end boundary.
                    continue


def build_provider(
    params: dict[str, Any],
    secrets: Optional[dict[str, str]] = None,
) -> SonioxTTSProvider:
    """Factory used by :func:`services.tts_realtime.build_chat_provider`."""
    api_key = (secrets or {}).get("api_key") or ""
    if not api_key:
        raise RuntimeError(
            "Soniox TTS selected but no API key configured. "
            "Set it under Settings → Voice → Soniox."
        )
    return SonioxTTSProvider(
        api_key=api_key,
        model=params.get("model") or DEFAULT_MODEL,
        voice=params.get("voice") or DEFAULT_VOICE,
        language=params.get("language") or DEFAULT_LANGUAGE,
        sample_rate=params.get("sample_rate") or DEFAULT_SAMPLE_RATE,
    )
