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
# Largest text size sent in ONE edge_tts request. A single oversized request
# (e.g. a whole ~9k-char story chapter) streams slower than real-time playback
# and intermittently returns "no audio" / truncates — which made long story
# chapters play "tậm tịt". Capping every chunk keeps each request small, fast
# and reliable. Tier 1/2 stay far below this for low time-to-first-byte.
EDGE_MAX_CHUNK = 500


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
        """Tiered split for low TTFB + reliable long-text synthesis.

        Tier 1: ~50 chars   (instant first audio)
        Tier 2: ~300 chars
        Tier 3+: the rest in ``EDGE_MAX_CHUNK`` (500)-char chunks — NOT one
        giant remainder chunk. A single oversized request streams slower than
        playback and intermittently returns no audio / truncates (that made
        long story chapters play "tậm tịt"); many small requests are fast and
        reliable.

        Splits at sentence > comma > whitespace boundaries to keep speech
        natural. Every returned chunk is <= ``EDGE_MAX_CHUNK`` chars.
        """
        text = text.strip()
        if len(text) <= 100:
            return [text]

        chunks: list[str] = []
        pos = 0
        i = 0

        while pos < len(text):
            # First two chunks stay tiny for low TTFB; everything after is
            # capped at EDGE_MAX_CHUNK so no single request goes oversized.
            limit = 50 if i == 0 else (300 if i == 1 else EDGE_MAX_CHUNK)
            i += 1

            end = min(pos + limit, len(text))
            if end >= len(text):
                tail = text[pos:].strip()
                if tail:
                    chunks.append(tail)
                break

            best = end
            search_start = max(pos, end - int(limit * 0.5))

            # Sentence break first, then comma, then whitespace.
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
            pos = best if best > pos else end  # guarantee forward progress

        return chunks

    async def _synth_chunk(self, chunk_text: str, *, attempts: int = 3) -> bytes:
        """Synthesize ONE text chunk to audio bytes, retrying transient
        empty/failed responses with linear backoff.

        Buffered (not streamed) on purpose: a failed attempt can then be
        retried cleanly without duplicating already-emitted audio. Chunks are
        <= ``EDGE_MAX_CHUNK`` chars so the buffering cost is negligible.
        Returns ``b''`` only if every attempt produced no audio.
        """
        last = "no audio received"
        for attempt in range(1, attempts + 1):
            buf = bytearray()
            try:
                communicate = edge_tts.Communicate(chunk_text, self.voice, rate=self.rate)
                async for chunk in communicate.stream():
                    if chunk.get("type") == "audio" and chunk.get("data"):
                        buf.extend(chunk["data"])
                if buf:
                    return bytes(buf)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = str(exc)
            if attempt < attempts:
                logger.warning(
                    "EdgeTTS chunk attempt %d/%d failed (%s) — retrying",
                    attempt, attempts, last,
                )
                await asyncio.sleep(0.4 * attempt)  # linear backoff
        logger.error("EdgeTTS chunk gave no audio after %d attempts (%s)", attempts, last)
        return b""

    async def stream_audio(self, text: str) -> AsyncIterator[bytes]:
        """Stream audio bytes with tiered chunking for low TTFB.

        Short text (<100 chars): single chunk.
        Long text: 50 → 300 → <=``EDGE_MAX_CHUNK`` chunks. Each chunk is
        synthesized with per-chunk retry (:meth:`_synth_chunk`). A chunk that
        yields no audio after all retries fails the whole stream so the caller
        drops the partial file instead of serving a silent gap (no silent
        fallback).
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
                data = await self._synth_chunk(chunk_text)
                if not data:
                    raise RuntimeError(
                        f"EdgeTTS produced no audio for chunk {i + 1}/{len(chunks)} "
                        f"after retries ({len(chunk_text)} chars)"
                    )
                if ttfb is None:
                    ttfb = time.perf_counter() - stream_start
                total_bytes += len(data)
                yield data

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


