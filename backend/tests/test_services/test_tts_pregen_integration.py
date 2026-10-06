"""Pre-gen ``execute_task`` integration — real file I/O, edge_tts faked only at
the network boundary.

Fences the atomic-publish guarantee: a present ``cache_path`` ALWAYS means a
complete chapter. A mid-chapter chunk failure must NOT leave a truncated file
that the cache-hit short-circuit then serves forever (the "tậm tịt" bug). The
unit tests in ``test_tts_chunking.py`` fake everything; these let the cleanup /
``bytes_written`` / atomic-rename file logic run for real.
"""
from __future__ import annotations

import os

import pytest

import services.tts_pregen_job as pg
import services.tts as tts_mod


class _FakeScheduler:
    async def check_pause_point(self):
        return None


class _ScriptedCommunicate:
    """Network-boundary fake. ``fail_on`` = a chunk text that yields no audio
    (every attempt); any other chunk yields ``b'AU'``."""

    fail_on: str | None = None

    def __init__(self, text, voice, rate=None):
        self.text = text

    async def stream(self):
        if self.text == type(self).fail_on:
            return  # no audio — simulates "No audio received"
        yield {"type": "audio", "data": b"AU"}


@pytest.fixture()
def _job(tmp_path, monkeypatch):
    # Deterministic chunking so the "3rd chunk" is identifiable.
    monkeypatch.setattr(
        tts_mod.EdgeTTSProvider, "_split_tiered",
        staticmethod(lambda text: ["A", "B", "C"]),
    )
    # Network boundary faked; file I/O is real.
    _ScriptedCommunicate.fail_on = None
    monkeypatch.setattr(pg.edge_tts, "Communicate", _ScriptedCommunicate)
    # Isolate the cache file in tmp_path.
    cache_path = str(tmp_path / "chap.mp3")
    monkeypatch.setattr(pg, "get_audio_cache_path", lambda text: cache_path)
    monkeypatch.setattr(pg, "clean_text_for_tts", lambda t: t)
    monkeypatch.setattr(pg.TTSPreGenJob, "_read_chapter_text", lambda self, s, c: "chapter body")
    monkeypatch.setattr(pg.TTSPreGenJob, "rescan", lambda self: None)
    # No real backoff / inter-chapter delay.
    async def _noop(_):
        return None
    monkeypatch.setattr(pg.asyncio, "sleep", _noop)

    job = pg.TTSPreGenJob(scheduler=_FakeScheduler())
    job.CHAPTER_DELAY = 0
    return job, cache_path


@pytest.mark.asyncio
async def test_happy_path_writes_complete_file(_job):
    job, cache_path = _job
    ok = await job.execute_task({"story_title": "S", "chapter_file": "c.txt"})
    assert ok is True
    assert os.path.exists(cache_path), "completed chapter must be published to cache_path"
    with open(cache_path, "rb") as f:
        data = f.read()
    assert data == b"AUAUAU", "all chunks concatenated, none dropped/duplicated"
    assert not os.path.exists(cache_path + ".tmp"), "temp file must be renamed away"
    assert not os.path.exists(cache_path + ".lock")


@pytest.mark.asyncio
async def test_midchapter_failure_leaves_no_cached_file(_job):
    # Chunks A, B succeed; C yields no audio on every retry → execute_task fails.
    # The bug: a partial cache_path would survive (bytes_written>0) and be served
    # forever. With atomic publish, cache_path must NOT exist.
    job, cache_path = _job
    _ScriptedCommunicate.fail_on = "C"

    ok = await job.execute_task({"story_title": "S", "chapter_file": "c.txt"})

    assert ok is False, "a mid-chapter failure must report failure"
    assert not os.path.exists(cache_path), (
        "a truncated chapter must NOT be left in cache (would be served forever)"
    )
    assert not os.path.exists(cache_path + ".tmp"), "temp file must be cleaned up"
    assert not os.path.exists(cache_path + ".lock")


@pytest.mark.asyncio
async def test_on_demand_joins_pregen_without_second_writer(_job):
    import asyncio
    from starlette.requests import Request
    from unittest.mock import MagicMock
    from services.tts_audio_response import audio_response
    from services.audio_generation import active_generation

    job, cache_path = _job
    gate = asyncio.Event()
    first = asyncio.Event()
    calls = []

    class Communicate:
        def __init__(self, text, voice, rate=None):
            self.text = text
        async def stream(self):
            calls.append(self.text)
            if self.text == 'B':
                first.set()
                await gate.wait()
            yield {'type': 'audio', 'data': b'AU'}

    pg.edge_tts.Communicate = Communicate
    producer = asyncio.create_task(job.execute_task({'story_title': 'Synthetic', 'chapter_file': '01.txt'}))
    await first.wait()
    generation = active_generation(cache_path)
    assert generation and generation.size == 2
    provider = MagicMock()
    response = await audio_response(
        cache_path, 'Synthetic body', 'story_synthetic_01.txt',
        Request({'type': 'http', 'method': 'GET', 'headers': []}),
        provider, {}, MagicMock(), None, None,
    )
    first_bytes = await anext(response.body_iterator)
    assert first_bytes == b'AU'
    gate.set()
    data = first_bytes + b''.join([c async for c in response.body_iterator])
    assert await producer
    assert data == b'AUAUAU'
    assert calls == ['A', 'B', 'C']
    provider.stream_audio.assert_not_called()
