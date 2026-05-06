"""TTS service — Edge TTS provider primitives.

Public API:
    - TTSProvider      (ABC implemented by every provider, including
                        :class:`services.tts_realtime.RealtimeTTSProvider`)
    - EdgeTTSProvider  (concrete; used by stories + as the chat default)

The active *chat* / *stories* providers are built from DB-backed JSON via
:mod:`services.tts_realtime` factories (``build_chat_provider`` /
``build_stories_provider``) and swapped into :mod:`services.shared_state`.
There is no env-var driven factory here — the registry is the source of
truth.
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


