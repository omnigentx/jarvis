"""Tests for services/tts.py — Edge TTS provider primitives."""
import os
from unittest.mock import patch

import pytest

from services.tts import (
    DEFAULT_EDGE_RATE,
    DEFAULT_EDGE_VOICE,
    EDGE_MAX_CHUNK,
    EdgeTTSProvider,
    TTSProvider,
)


class TestEdgeTTSProviderEmptyInputs:
    """generate_audio / stream_audio must short-circuit on empty input."""

    @pytest.mark.asyncio
    async def test_generate_audio_empty_returns_none(self):
        provider = EdgeTTSProvider()
        assert await provider.generate_audio("") is None

    @pytest.mark.asyncio
    async def test_generate_audio_whitespace_returns_none(self):
        provider = EdgeTTSProvider()
        assert await provider.generate_audio("   \n\t  ") is None

    @pytest.mark.asyncio
    async def test_generate_audio_base64_empty_returns_none(self):
        provider = EdgeTTSProvider()
        assert await provider.generate_audio_base64("") is None

    @pytest.mark.asyncio
    async def test_stream_audio_empty_yields_nothing(self):
        provider = EdgeTTSProvider()
        chunks = [c async for c in provider.stream_audio("")]
        assert chunks == []

    @pytest.mark.asyncio
    async def test_stream_audio_whitespace_yields_nothing(self):
        provider = EdgeTTSProvider()
        chunks = [c async for c in provider.stream_audio("   ")]
        assert chunks == []


class TestEdgeTTSProviderTieredSplit:
    """Cover the static splitter that controls TTFB chunking."""

    def test_short_text_returns_single_chunk(self):
        text = "Xin chào."
        assert EdgeTTSProvider._split_tiered(text) == [text]

    def test_text_at_threshold_returns_single_chunk(self):
        # 100 chars or less → no splitting
        text = "a" * 100
        assert EdgeTTSProvider._split_tiered(text) == [text]

    def test_long_text_splits_into_bounded_chunks(self):
        text = "Câu một. " * 100  # ~900 chars
        chunks = EdgeTTSProvider._split_tiered(text)
        # Tier 1/2 stay small for TTFB; the rest is capped at EDGE_MAX_CHUNK —
        # NOT lumped into one giant remainder chunk (that oversized request is
        # what made long story chapters play "tậm tịt").
        assert len(chunks) >= 3
        assert all(len(c) <= EDGE_MAX_CHUNK for c in chunks)
        assert len(chunks[0]) <= 50
        # No content lost (modulo whitespace stripping)
        joined_len = sum(len(c) for c in chunks)
        assert joined_len <= len(text)

    def test_split_prefers_sentence_boundaries(self):
        # Build a sentence-rich block; tier 1 should end at a sentence boundary.
        text = "Một câu. " * 60
        chunks = EdgeTTSProvider._split_tiered(text)
        assert len(chunks) >= 2
        for chunk in chunks[:-1]:
            assert chunk.endswith(("..", ".", "?", "!", ",")) or chunk.endswith(" ".strip())

    def test_split_strips_whitespace(self):
        text = "   " + ("Một câu dài. " * 60) + "   "
        chunks = EdgeTTSProvider._split_tiered(text)
        for chunk in chunks:
            assert chunk == chunk.strip()
            assert chunk  # never empty


class TestEdgeTTSProviderGenerateAudio:
    """generate_audio happy path uses a temp file — verify cleanup."""

    @pytest.mark.asyncio
    async def test_generate_audio_returns_bytes_and_cleans_temp(self, tmp_path, monkeypatch):
        # Force tempfile.gettempdir to a controlled location.
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

        class _FakeCommunicate:
            def __init__(self, text, voice, rate):
                self.text = text

            async def save(self, path):
                with open(path, "wb") as f:
                    f.write(b"FAKE_MP3_BYTES")

        with patch("services.tts.edge_tts.Communicate", _FakeCommunicate):
            provider = EdgeTTSProvider()
            result = await provider.generate_audio("hello")

        assert result == b"FAKE_MP3_BYTES"
        # Temp file must be cleaned up
        assert not any(tmp_path.iterdir()), "temp file not cleaned up"

    @pytest.mark.asyncio
    async def test_generate_audio_cleans_temp_on_exception(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

        class _BoomCommunicate:
            def __init__(self, *a, **kw):
                pass

            async def save(self, path):
                # Touch the file then fail — simulates partial write.
                with open(path, "wb") as f:
                    f.write(b"partial")
                raise RuntimeError("boom")

        with patch("services.tts.edge_tts.Communicate", _BoomCommunicate):
            provider = EdgeTTSProvider()
            result = await provider.generate_audio("hello")

        assert result is None
        assert not any(tmp_path.iterdir()), "temp file not cleaned up after error"


# Factory tests live alongside their owners now:
#   - chat / stories factory routing → tests/test_services/test_tts_split.py
#   - registry-driven config + listener dispatch → test_runtime_config_voice.py
# The legacy env-var TTSFactory was removed in favour of DB-backed JSON config.
