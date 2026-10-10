"""Private, bounded chunk checkpoints shared by Edge pregen and playback.

Final MP3 publication remains the audio generation lease owner's responsibility.
Checkpoints contain audio and integrity metadata, never chapter prose/names.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from services.tts import EdgeTTSProvider, TTSProvider

logger = logging.getLogger(__name__)
RETENTION_SECONDS = 7 * 86400
MAX_CACHE_BYTES = 256 * 1024 * 1024
MAX_CHUNK_BYTES = 4 * 1024 * 1024


def _identity(provider: EdgeTTSProvider, text: str) -> str:
    chunks = provider._split_tiered(text)
    data = json.dumps(['edge-chunks-v1', provider.voice, provider.rate, text, chunks],
                      ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(data).hexdigest()


class ChunkCheckpoints:
    def __init__(self, path: str, identity: str):
        self.root = Path(path).parent / '.chunks'
        self.directory = self.root / identity
        self.lease = None

    def open(self) -> 'ChunkCheckpoints':
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.root / '.gc.lock').open('a+b') as gc:
            fcntl.flock(gc, fcntl.LOCK_EX)
            self.directory.mkdir(mode=0o700, exist_ok=True)
            self.lease = (self.directory / '.lease').open('a+b')
            try:
                fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._clean()
            except BaseException:
                self.close()
                raise
        return self

    def _clean(self) -> None:
        candidates = []
        total = 0
        for directory in self.root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            size = 0
            for file in directory.iterdir():
                try:
                    if file.is_file():
                        size += file.stat().st_size
                except FileNotFoundError:
                    continue  # Another active writer atomically published its temp file.
            total += size
            candidates.append((directory.stat().st_mtime, directory, size))
        for touched, directory, size in sorted(candidates):
            if directory == self.directory:
                continue
            if time.time() - touched <= RETENTION_SECONDS and total <= MAX_CACHE_BYTES:
                continue
            with (directory / '.lease').open('a+b') as lease:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue  # Never evict another writer's active checkpoint.
                shutil.rmtree(directory)
                total -= size

    def load(self, index: int) -> bytes | None:
        audio = self.directory / f'{index:04d}.mp3'
        metadata = audio.with_suffix('.json')
        try:
            if audio.stat().st_size > MAX_CHUNK_BYTES:
                return None
            data = audio.read_bytes()
            info = json.loads(metadata.read_text())
            if (data and len(data) == info['size']
                    and hashlib.sha256(data).hexdigest() == info['sha256']):
                os.utime(self.directory, None)
                return data
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def save(self, index: int, data: bytes) -> None:
        if not data or len(data) > MAX_CHUNK_BYTES:
            raise ValueError('Invalid speech chunk size')
        # A single chapter cannot grow this auxiliary cache without bound.
        size = sum(f.stat().st_size for f in self.directory.glob('*.mp3'))
        if size + len(data) > MAX_CACHE_BYTES:
            raise RuntimeError('Speech checkpoint quota exceeded')
        audio = self.directory / f'{index:04d}.mp3'
        metadata = audio.with_suffix('.json')
        temp = audio.with_suffix('.tmp')
        with temp.open('wb') as output:
            os.chmod(temp, 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, audio)
        temp = metadata.with_suffix('.json.tmp')
        temp.write_text(json.dumps({'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}))
        os.chmod(temp, 0o600)
        os.replace(temp, metadata)
        os.utime(self.directory, None)

    def discard(self) -> None:
        with (self.root / '.gc.lock').open('a+b') as gc:
            fcntl.flock(gc, fcntl.LOCK_EX)
            shutil.rmtree(self.directory)

    def close(self) -> None:
        if self.lease:
            self.lease.close()
            self.lease = None


async def _disk_operation(function, *args, cleanup=None):
    """Finish an atomic disk operation before releasing its writer lease."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            result = await task
            if cleanup:
                cleanup(result)
        finally:
            raise


async def stream_story_audio(provider: TTSProvider, text: str, path: str,
                             before_chunk: Callable[[], Awaitable[None]] | None = None,
                             on_progress: Callable[[int, int], None] | None = None
                             ) -> AsyncIterator[bytes]:
    if not isinstance(provider, EdgeTTSProvider):
        async for data in provider.stream_audio(text):
            yield data
        return
    store = await _disk_operation(ChunkCheckpoints(path, _identity(provider, text)).open,
                                  cleanup=lambda result: result.close())
    try:
        chunks = provider._split_tiered(text)
        for index, chunk in enumerate(chunks):
            if before_chunk:
                await before_chunk()
            data = await _disk_operation(store.load, index)
            if data is None:
                data = await provider._synth_chunk(chunk)
                if not data:
                    raise RuntimeError(f'Edge TTS failed at chunk {index + 1}/{len(chunks)}')
                await _disk_operation(store.save, index, data)
            if on_progress:
                on_progress(index + 1, len(chunks))
            yield data
    finally:
        store.close()


def discard_checkpoints(provider: TTSProvider, text: str, path: str) -> None:
    if not isinstance(provider, EdgeTTSProvider):
        return
    store = ChunkCheckpoints(path, _identity(provider, text)).open()
    try:
        store.discard()
    finally:
        store.close()
