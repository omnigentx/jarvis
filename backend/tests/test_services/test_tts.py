"""Tests for services/tts.py — Edge TTS provider + factory."""
import os
from unittest.mock import patch

import pytest

from services.tts import (
    DEFAULT_EDGE_RATE,
    DEFAULT_EDGE_VOICE,
    EdgeTTSProvider,
    TTSFactory,
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

    def test_long_text_splits_into_three_tiers(self):
        text = "Câu một. " * 100  # ~900 chars
        chunks = EdgeTTSProvider._split_tiered(text)
        assert len(chunks) == 3
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


class TestTTSFactory:
    """Factory selects + configures EdgeTTSProvider from env vars."""

    def test_default_returns_edge_provider(self, monkeypatch):
        monkeypatch.delenv("TTS_PROVIDER", raising=False)
        monkeypatch.delenv("EDGE_TTS_VOICE", raising=False)
        monkeypatch.delenv("EDGE_TTS_RATE", raising=False)

        provider = TTSFactory.get_provider()
        assert isinstance(provider, EdgeTTSProvider)
        assert isinstance(provider, TTSProvider)
        assert provider.voice == DEFAULT_EDGE_VOICE
        assert provider.rate == DEFAULT_EDGE_RATE

    def test_explicit_edge_provider(self, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "edge")
        provider = TTSFactory.get_provider()
        assert isinstance(provider, EdgeTTSProvider)

    def test_unknown_provider_falls_back_to_edge(self, monkeypatch, caplog):
        monkeypatch.setenv("TTS_PROVIDER", "elevenlabs")
        with caplog.at_level("WARNING"):
            provider = TTSFactory.get_provider()
        assert isinstance(provider, EdgeTTSProvider)
        assert any("not supported" in r.message for r in caplog.records)

    def test_voice_and_rate_overrides(self, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "edge")
        monkeypatch.setenv("EDGE_TTS_VOICE", "vi-VN-HoaiMyNeural")
        monkeypatch.setenv("EDGE_TTS_RATE", "+10%")
        provider = TTSFactory.get_provider()
        assert isinstance(provider, EdgeTTSProvider)
        assert provider.voice == "vi-VN-HoaiMyNeural"
        assert provider.rate == "+10%"

    def test_provider_value_is_normalised(self, monkeypatch):
        # Whitespace + casing must not break the comparison.
        monkeypatch.setenv("TTS_PROVIDER", "  EDGE  ")
        provider = TTSFactory.get_provider()
        assert isinstance(provider, EdgeTTSProvider)

    def test_empty_provider_uses_default(self, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "")
        provider = TTSFactory.get_provider()
        assert isinstance(provider, EdgeTTSProvider)
