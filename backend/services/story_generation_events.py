"""Push on-demand speech progress through the same story pregen channel."""
from __future__ import annotations

import time
from core.database import StoryChapter, get_db_session
from helpers.audio_cache import get_content_hash
from services.pregen_stream import pregen_stream_manager


def chapter_targets(text: str) -> list[dict]:
    with get_db_session() as db:
        return [{'story_id': row.story_id, 'chapter_file': row.chapter_file}
                for row in db.query(StoryChapter).filter(
                    StoryChapter.content_hash == get_content_hash(text)).all()]


class StoryGenerationEvents:
    def __init__(self, targets: list[dict]):
        self.targets = targets
        self.completed = self.total = 0
        self.terminal = False

    def emit(self, event: str, **details) -> None:
        if event in ('chapter_ready', 'chapter_error', 'chapter_pending'):
            self.terminal = True
        for target in self.targets:
            pregen_stream_manager.broadcast({'type': event, **target, **details})

    def progress(self, completed: int, total: int) -> None:
        self.completed, self.total = completed, total
        self.emit('chapter_progress', completed_chunks=completed, total_chunks=total)

    def failed(self, delay: int) -> None:
        self.emit('chapter_error', retry_after=delay, retry_at=time.time() + delay,
                  completed_chunks=self.completed, total_chunks=self.total)
