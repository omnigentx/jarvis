"""EdgeTTS chunking + per-chunk retry — the fix for story audio playing
"tậm tịt" (intermittent).

Root cause being fenced here: the old ``_split_tiered`` lumped everything past
~350 chars into ONE giant chunk, so a long story chapter became a single
oversized ``edge_tts`` request that streamed slower than playback and
intermittently returned no audio / truncated. Now every chunk is capped at
``EDGE_MAX_CHUNK`` and each is synthesized with retry.
"""
from __future__ import annotations

import asyncio

import pytest

from services.tts import EdgeTTSProvider, EDGE_MAX_CHUNK
import services.tts as tts_mod


# ── _split_tiered: bounded chunks ─────────────────────────────────────


def test_short_text_single_chunk():
    assert EdgeTTSProvider._split_tiered("Xin chào.") == ["Xin chào."]


def test_every_chunk_within_max():
    text = ("Câu văn dài. " * 400).strip()  # ~5200 chars, no giant remainder
    chunks = EdgeTTSProvider._split_tiered(text)
    assert len(chunks) > 5, "long text must be split into many chunks, not one"
    assert all(len(c) <= EDGE_MAX_CHUNK for c in chunks), (
        f"a chunk exceeded EDGE_MAX_CHUNK={EDGE_MAX_CHUNK}: "
        f"max={max(len(c) for c in chunks)}"
    )


def test_first_two_chunks_small_for_ttfb():
    text = "A" * 4000
    chunks = EdgeTTSProvider._split_tiered(text)
    assert len(chunks[0]) <= 50
    assert len(chunks[1]) <= 300


def test_no_text_dropped():
    # Concatenating chunks (ignoring boundary whitespace) reproduces the text.
    text = "Chương một. " + "Nội dung kể chuyện rất dài đây. " * 200
    chunks = EdgeTTSProvider._split_tiered(text)
    joined = "".join(chunks)
    assert joined.replace(" ", "") == text.strip().replace(" ", "")


# ── _synth_chunk: per-chunk retry ─────────────────────────────────────


class _FakeCommunicate:
    """Drives a scripted sequence of attempt outcomes. ``script`` items:
    ``"ok"`` -> yields audio, ``"empty"`` -> yields nothing, ``"raise"`` ->
    raises, ``"cancel"`` -> raises CancelledError."""

    script: list = []
    calls: int = 0

    def __init__(self, text, voice, rate=None):
        self.text = text

    async def stream(self):
        outcome = type(self).script[type(self).calls]
        type(self).calls += 1
        if outcome == "raise":
            raise RuntimeError("No audio was received. Please verify ... parameters")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        if outcome == "ok":
            yield {"type": "audio", "data": b"AUDIO"}
        # "empty" yields nothing


@pytest.fixture()
def _fast_retry(monkeypatch):
    # Don't actually sleep through the backoff.
    async def _noop(_):
        return None
    monkeypatch.setattr(tts_mod.asyncio, "sleep", _noop)
    _FakeCommunicate.calls = 0
    monkeypatch.setattr(tts_mod.edge_tts, "Communicate", _FakeCommunicate)


@pytest.mark.asyncio
async def test_synth_chunk_retries_then_succeeds(_fast_retry):
    _FakeCommunicate.script = ["raise", "empty", "ok"]
    out = await EdgeTTSProvider()._synth_chunk("hello", attempts=3)
    assert out == b"AUDIO"
    assert _FakeCommunicate.calls == 3, "should have retried until the 3rd attempt"


@pytest.mark.asyncio
async def test_synth_chunk_returns_empty_after_all_attempts(_fast_retry):
    _FakeCommunicate.script = ["empty", "raise", "empty"]
    out = await EdgeTTSProvider()._synth_chunk("hello", attempts=3)
    assert out == b""


@pytest.mark.asyncio
async def test_synth_chunk_reraises_cancel_without_retry(_fast_retry):
    _FakeCommunicate.script = ["cancel", "ok", "ok"]
    with pytest.raises(asyncio.CancelledError):
        await EdgeTTSProvider()._synth_chunk("hello", attempts=3)
    assert _FakeCommunicate.calls == 1, "cancel must NOT be retried"


@pytest.mark.asyncio
async def test_stream_audio_fails_loud_on_unrecoverable_chunk(_fast_retry):
    # All attempts empty → stream_audio must raise (no silent gap), not yield
    # a partial/empty stream the caller would serve as a truncated file.
    _FakeCommunicate.script = ["empty"] * 9
    with pytest.raises(Exception):
        async for _ in EdgeTTSProvider().stream_audio("a longer line of text here"):
            pass
