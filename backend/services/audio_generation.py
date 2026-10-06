"""Single writer per audio cache, shared by on-demand and pre-generation.

Only complete audio is published to .mp3. Readers follow writer notifications,
not file/status polling. An OS lock excludes writers in other worker processes.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, AsyncIterator

from services.tts import EDGE_CHUNK_RETRY_BUDGET

# Reader must not abandon a healthy producer between completed chunks.
PROGRESS_TIMEOUT = float(EDGE_CHUNK_RETRY_BUDGET + 10)


class GenerationBusy(RuntimeError):
    """Another process owns this audio. The caller may retry later."""


@dataclass
class AudioGeneration:
    path: str
    lease: BinaryIO
    size: int = 0
    done: bool = False
    error: bool = False
    revision: int = 0
    task: asyncio.Task[None] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def temporary_path(self) -> str:
        return self.path + '.tmp'

    def notify(self, size: int) -> None:
        self.size = size
        self.revision += 1
        previous, self.changed = self.changed, asyncio.Event()
        previous.set()

    async def wait(self, revision: int) -> None:
        if self.revision == revision and not self.done:
            await asyncio.wait_for(self.changed.wait(), PROGRESS_TIMEOUT)

    def finish(self, success: bool) -> None:
        if self.done:
            return
        self.done = True
        self.error = not success
        try:
            if not success:
                Path(self.temporary_path).unlink(missing_ok=True)
            Path(self.path + '.lock').unlink(missing_ok=True)
        finally:
            self.lease.close()
            self.notify(self.size)
            if _active.get(self.path) is self:
                del _active[self.path]

    async def stream(self) -> AsyncIterator[bytes]:
        """Open a stable inode before awaiting; atomic publish preserves it."""
        while not self.size and not self.done:
            await self.wait(self.revision)
        if self.error:
            raise RuntimeError('Audio generation failed')
        source = self.path if self.done else self.temporary_path
        with open(source, 'rb') as audio:
            offset = 0
            while True:
                revision = self.revision
                # Flush happens before notify, so each announced byte is readable.
                data = audio.read(min(8192, max(0, self.size - offset)))
                if data:
                    offset += len(data)
                    yield data
                    continue
                if self.done:
                    if self.error:
                        raise RuntimeError('Audio generation failed')
                    return
                await self.wait(revision)


_active: dict[str, AudioGeneration] = {}


def active_generation(path: str) -> AudioGeneration | None:
    return _active.get(path)


def claim_generation(path: str) -> tuple[AudioGeneration, bool]:
    """Claim synchronously before the first await: concurrent GETs cannot race."""
    existing = _active.get(path)
    if existing:
        return existing, False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lease = open(path + '.lock', 'a+b')
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lease.close()
        raise GenerationBusy('Audio is being generated in another worker') from exc
    # A successful lease recovers an interrupted producer's temporary file.
    try:
        Path(path + '.tmp').unlink(missing_ok=True)
    except BaseException:
        lease.close()
        raise
    generation = AudioGeneration(path, lease)
    _active[path] = generation
    return generation, True
