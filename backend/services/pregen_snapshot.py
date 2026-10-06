"""Authoritative reconnect snapshot for pregen SSE (no chapter prose reads)."""
from __future__ import annotations

import time

from core.database import StoryChapter, get_db_session
from services.audio_retry import AudioRetry, ensure_schema, profile_hash
from services.tts import DEFAULT_EDGE_RATE, DEFAULT_EDGE_VOICE


def failure_snapshot(story_id: str | None = None) -> list[dict]:
    with get_db_session() as db:
        ensure_schema(db)
        query = (db.query(StoryChapter, AudioRetry)
                 .join(AudioRetry, StoryChapter.content_hash == AudioRetry.content_hash)
                 .filter(StoryChapter.status == 'failed',
                         AudioRetry.profile == profile_hash(DEFAULT_EDGE_VOICE, DEFAULT_EDGE_RATE)))
        if story_id:
            query = query.filter(StoryChapter.story_id == story_id)
        return [{'story_id': chapter.story_id, 'chapter_file': chapter.chapter_file,
                 'retry_at': retry.retry_at, 'retry_after': max(0, retry.retry_at - time.time())}
                for chapter, retry in query.all()]


def ready_snapshot(story_id: str | None = None) -> list[str]:
    with get_db_session() as db:
        query = db.query(StoryChapter.chapter_file).filter(StoryChapter.status == 'ready')
        if story_id:
            query = query.filter(StoryChapter.story_id == story_id)
        return [name for (name,) in query.all()]
