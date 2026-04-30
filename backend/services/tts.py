"""TTS service — Edge TTS only (free, no API key).

ElevenLabs and Gemini providers were removed per BRD D6. They will be
re-implemented properly via the Settings UI when needed.

Public API (do not break — many routes/services import these):
    - TTSProvider      (ABC)
    - EdgeTTSProvider  (concrete)
    - TTSFactory       (singleton-style factory)

Environment variables:
    TTS_PROVIDER     Only ``edge`` is accepted. Other values log a warning
                     and fall back to edge.
    EDGE_TTS_VOICE   edge-tts voice id (default: vi-VN-NamMinhNeural).
    EDGE_TTS_RATE    edge-tts rate string, e.g. ``+20%`` (default: +20%).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

import edge_tts

logger = logging.getLogger(__name__)

DEFAULT_EDGE_VOICE = "vi-VN-NamMinhNeural"
DEFAULT_EDGE_RATE = "+20%"


class TTSProvider(ABC):
    """Abstract TTS provider — kept as a base class so future providers can plug in."""

    @abstractmethod
    async def generate_audio(self, text: str) -> Optional[bytes]:
        """Return full audio bytes for ``text``, or ``None`` if nothing to synthesise."""

    async def stream_audio(self, text: str) -> AsyncIterator[bytes]:
        """Stream audio bytes. Default impl yields the full payload at once."""
        audio_bytes = await self.generate_audio(text)
        if audio_bytes:
            yield audio_bytes

    async def generate_audio_base64(self, text: str) -> Optional[str]:
        audio_bytes = await self.generate_audio(text)
        if audio_bytes is None:
            return None
        return base64.b64encode(audio_bytes).decode("utf-8")


class EdgeTTSProvider(TTSProvider):
    """Microsoft Edge TTS — free, no API key, native Vietnamese voices."""

    def __init__(
        self,
        voice: str = DEFAULT_EDGE_VOICE,
        rate: str = DEFAULT_EDGE_RATE,
    ) -> None:
        self.voice = voice
        self.rate = rate

    async def generate_audio(self, text: str) -> Optional[bytes]:
        if not text or not text.strip():
            return None

        # Use tempfile + try/finally so the file is always removed, even on error
        # or cancellation. edge-tts only exposes ``save(path)``.
        tmp_dir = tempfile.gettempdir()
        path = os.path.join(tmp_dir, f"jarvis-edge-tts-{uuid.uuid4().hex}.mp3")
        try:
            communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)
            await communicate.save(path)
            with open(path, "rb") as f:
                return f.read()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("EdgeTTS generate_audio failed: %s", exc)
            return None
        finally:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                # Best-effort cleanup; never raise from a finally block.
                pass

    @staticmethod
    def _split_tiered(text: str) -> list[str]:
        """3-tier split for low TTFB.

        Tier 1: ~50 chars  (instant first audio)
        Tier 2: ~300 chars
        Tier 3: remaining text in a single chunk.

        Splits at sentence > comma > whitespace boundaries to keep speech natural.
        """
        text = text.strip()
        if len(text) <= 100:
            return [text]

        tiers = (50, 300)
        chunks: list[str] = []
        pos = 0

        for limit in tiers:
            if pos >= len(text):
                break

            end = min(pos + limit, len(text))
            if end >= len(text):
                tail = text[pos:].strip()
                if tail:
                    chunks.append(tail)
                pos = len(text)
                break

            best = end
            search_start = max(pos, end - int(limit * 0.5))

            # Sentence break first
            for punct in (". ", "? ", "! ", ".\n", "?\n", "!\n"):
                idx = text.rfind(punct, search_start, end)
                if idx != -1:
                    best = idx + 1
                    break
            else:
                idx = text.rfind(", ", search_start, end)
                if idx != -1:
                    best = idx + 1
                else:
                    idx = text.rfind(" ", search_start, end)
                    if idx != -1:
                        best = idx + 1

            chunk = text[pos:best].strip()
            if chunk:
                chunks.append(chunk)
            pos = best

        if pos < len(text):
            remaining = text[pos:].strip()
            if remaining:
                chunks.append(remaining)

        return chunks

    async def stream_audio(self, text: str) -> AsyncIterator[bytes]:
        """Stream audio bytes with 3-tier chunking for low TTFB.

        Short text (<100 chars): single ``Communicate()`` call.
        Long text: 50 → 300 → rest. Bytes are yielded as they arrive.
        """
        if not text or not text.strip():
            return

        stream_start = time.perf_counter()
        ttfb: Optional[float] = None
        total_bytes = 0

        try:
            chunks = self._split_tiered(text)
            logger.debug(
                "EdgeTTS tiered split: %d chunks, sizes=%s",
                len(chunks),
                [len(c) for c in chunks],
            )

            for i, chunk_text in enumerate(chunks):
                chunk_start = time.perf_counter()
                chunk_ttfb: Optional[float] = None
                chunk_bytes = 0

                communicate = edge_tts.Communicate(chunk_text, self.voice, rate=self.rate)

                async for chunk in communicate.stream():
                    if chunk.get("type") != "audio":
                        continue
                    data: bytes = chunk["data"]
                    if not data:
                        continue
                    if chunk_ttfb is None:
                        chunk_ttfb = time.perf_counter() - chunk_start
                        if ttfb is None:
                            ttfb = time.perf_counter() - stream_start
                            logger.debug(
                                "EdgeTTS TTFB: %.0fms (chunk 1/%d)",
                                ttfb * 1000,
                                len(chunks),
                            )
                    chunk_bytes += len(data)
                    yield data

                total_bytes += chunk_bytes
                logger.debug(
                    "EdgeTTS chunk %d/%d done | %d chars | TTFB %.0fms | %dB",
                    i + 1,
                    len(chunks),
                    len(chunk_text),
                    (chunk_ttfb or 0.0) * 1000,
                    chunk_bytes,
                )

            total_duration = time.perf_counter() - stream_start
            logger.info(
                "EdgeTTS stream done | TTFB %.0fms | total %.0fms | %dB | %d chunks",
                (ttfb or 0.0) * 1000,
                total_duration * 1000,
                total_bytes,
                len(chunks),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("EdgeTTS stream failed: %s", exc)
            raise


class TTSFactory:
    """Factory used by ``shared_state`` and other callers.

    Kept as a class with a ``get_provider`` static method to preserve the existing
    API surface — any future providers will be added here.
    """

    @staticmethod
    def get_provider() -> TTSProvider:
        provider_type = (os.getenv("TTS_PROVIDER") or "edge").strip().lower()
        if provider_type and provider_type != "edge":
            logger.warning(
                "TTS_PROVIDER=%r is not supported (only 'edge'). Falling back to Edge TTS.",
                provider_type,
            )

        voice = os.getenv("EDGE_TTS_VOICE", DEFAULT_EDGE_VOICE)
        rate = os.getenv("EDGE_TTS_RATE", DEFAULT_EDGE_RATE)
        return EdgeTTSProvider(voice=voice, rate=rate)
