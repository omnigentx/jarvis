"""Checkpoint integrity, process restart, cancellation, identity and garbage collection."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from services import tts_checkpoints as cp
from services.tts import EdgeTTSProvider


def store(tmp_path, identity='a' * 64):
    return cp.ChunkCheckpoints(str(tmp_path / 'audio.mp3'), identity).open()


def test_restart_reuses_only_verified_audio_and_metadata_has_no_text(tmp_path):
    one = store(tmp_path)
    one.save(0, b'complete synthetic speech')
    directory = one.directory
    one.close()
    two = store(tmp_path)
    try:
        assert two.load(0) == b'complete synthetic speech'
        info = json.loads((directory / '0000.json').read_text())
        assert set(info) == {'sha256', 'size'}
        (directory / '0000.mp3').write_bytes(b'corrupted')
        assert two.load(0) is None
        two.save(0, b'replacement')
        (directory / '0000.json').write_text('{invalid json')
        assert two.load(0) is None
    finally:
        two.close()


@pytest.mark.parametrize('changed', ['voice', 'rate', 'text', 'partition'])
def test_changed_synthesis_identity_never_reuses_checkpoint(tmp_path, monkeypatch, changed):
    provider = EdgeTTSProvider()
    text = 'synthetic text'
    before = cp._identity(provider, text)
    if changed == 'voice': provider.voice = 'vi-VN-HoaiMyNeural'
    if changed == 'rate': provider.rate = '+0%'
    if changed == 'text': text += ' changed'
    if changed == 'partition': monkeypatch.setattr(provider, '_split_tiered', lambda _: ['new partition'])
    assert cp._identity(provider, text) != before


def test_expired_and_overquota_cache_evicts_idle_but_preserves_active(tmp_path, monkeypatch):
    active = store(tmp_path, 'a' * 64)
    active.save(0, b'active')
    idle = store(tmp_path, 'b' * 64)
    idle.save(0, b'idle')
    idle.close()
    os.utime(idle.directory, (1, 1))
    current = store(tmp_path, 'c' * 64)
    try:
        assert active.directory.exists()
        assert not idle.directory.exists()
    finally:
        current.close(); active.close()
    monkeypatch.setattr(cp, 'MAX_CACHE_BYTES', 1)
    new = store(tmp_path, 'd' * 64)
    try:
        assert not active.directory.exists()
    finally:
        new.close()


def test_same_checkpoint_cannot_have_two_writers(tmp_path):
    one = store(tmp_path)
    try:
        with pytest.raises(BlockingIOError): store(tmp_path)
    finally:
        one.close()


@pytest.mark.asyncio
async def test_cancel_retains_finished_chunk_and_retry_only_synthesizes_missing(tmp_path, monkeypatch):
    provider = EdgeTTSProvider()
    monkeypatch.setattr(provider, '_split_tiered', lambda _: ['A', 'B'])
    calls = []
    async def synth(text):
        calls.append(text)
        return text.encode()
    monkeypatch.setattr(provider, '_synth_chunk', synth)
    path = str(tmp_path / 'full.mp3')
    first = cp.stream_story_audio(provider, 'synthetic chapter', path)
    assert await anext(first) == b'A'
    await first.aclose()
    assert b''.join([x async for x in cp.stream_story_audio(provider, 'synthetic chapter', path)]) == b'AB'
    assert calls == ['A', 'B']
    cp.discard_checkpoints(provider, 'synthetic chapter', path)
    assert not list((tmp_path / '.chunks').glob('*/0000.mp3'))


@pytest.mark.asyncio
async def test_cancellation_during_disk_open_releases_lease(tmp_path, monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    original = cp.ChunkCheckpoints.open
    opened = []
    def delayed(self):
        result = original(self)
        opened.append(result)
        entered.set()
        assert release.wait(2)
        return result
    monkeypatch.setattr(cp.ChunkCheckpoints, 'open', delayed)
    generator = cp.stream_story_audio(EdgeTTSProvider(), 'synthetic', str(tmp_path / 'full.mp3'))
    consumer = asyncio.create_task(anext(generator))
    assert await asyncio.to_thread(entered.wait, 2)
    consumer.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError): await consumer
    assert opened[0].lease is None
    # Cancellation cannot leave the durable cache permanently locked.
    original(opened[0]).close()


def test_cleanup_tolerates_another_writer_renaming_temporary_chunk(tmp_path, monkeypatch):
    active = store(tmp_path)
    temporary = active.directory / 'chunk.tmp'
    temporary.write_bytes(b'pending')
    original = Path.stat
    counts = 0
    def racing_stat(path, *args, **kwargs):
        nonlocal counts
        if path == temporary:
            counts += 1
            if counts == 2:
                raise FileNotFoundError('writer published temporary chunk')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', racing_stat)
    try:
        other = store(tmp_path, 'b' * 64)
        other.close()
    finally:
        active.close()
