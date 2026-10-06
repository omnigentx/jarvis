"""A live, progressing Edge stream must survive the old total-time deadline."""
import asyncio

import pytest

from services import tts


@pytest.mark.asyncio
async def test_audio_progress_survives_old_total_deadline(monkeypatch):
    class Progressing:
        def __init__(self, *args, **kwargs):
            pass

        async def stream(self):
            for _ in range(6):
                await asyncio.sleep(0.01)
                yield {'type': 'audio', 'data': b'audio'}

    monkeypatch.setattr(tts.edge_tts, 'Communicate', Progressing)
    monkeypatch.setattr(tts, 'EDGE_CHUNK_TIMEOUT', 0.03)
    assert await tts.EdgeTTSProvider()._synth_chunk('synthetic', attempts=1) == b'audio' * 6


@pytest.mark.asyncio
async def test_metadata_does_not_keep_stalled_audio_alive(monkeypatch):
    class MetadataOnly:
        def __init__(self, *args, **kwargs):
            pass

        async def stream(self):
            while True:
                await asyncio.sleep(0.005)
                yield {'type': 'WordBoundary', 'text': 'synthetic'}

    monkeypatch.setattr(tts.edge_tts, 'Communicate', MetadataOnly)
    monkeypatch.setattr(tts, 'EDGE_CHUNK_TIMEOUT', 0.03)
    assert await asyncio.wait_for(tts.EdgeTTSProvider()._synth_chunk('synthetic', attempts=1), 0.2) == b''


@pytest.mark.asyncio
async def test_total_deadline_bounds_even_continuous_audio_and_closes_stream(monkeypatch):
    closed = []
    class Endless:
        def __init__(self, *args, **kwargs):
            pass
        async def stream(self):
            try:
                while True:
                    await asyncio.sleep(0.005)
                    yield {'type': 'audio', 'data': b'audio'}
            finally:
                closed.append(True)
    monkeypatch.setattr(tts.edge_tts, 'Communicate', Endless)
    monkeypatch.setattr(tts, 'EDGE_CHUNK_TIMEOUT', 0.03)
    monkeypatch.setattr(tts, 'EDGE_CHUNK_TOTAL_TIMEOUT', 0.06)
    assert await asyncio.wait_for(tts.EdgeTTSProvider()._synth_chunk('synthetic', attempts=1), 0.2) == b''
    assert closed == [True]
