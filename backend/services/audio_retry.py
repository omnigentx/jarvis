"""Persisted speech failure cooldowns, scoped to content and synthesis profile."""
from __future__ import annotations

import hashlib
import math
import threading
import time
import weakref

from sqlalchemy import Column, Float, Integer, String, select
from sqlalchemy.schema import CreateTable, CreateIndex
from sqlalchemy.orm import Session

from core.database import Base, StoryChapter, get_db_session
from helpers.audio_cache import get_content_hash

RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 900
RETENTION_SECONDS = 7 * 86400
_schema_lock = threading.Lock()
_schema_engines = weakref.WeakSet()


class AudioRetry(Base):
    """No source text or upstream exception messages are persisted."""
    __tablename__ = 'audio_retries'
    key = Column(String(64), primary_key=True)
    content_hash = Column(String(64), nullable=False, index=True)
    profile = Column(String(64), nullable=False)
    failures = Column(Integer, nullable=False, default=0)
    retry_at = Column(Float, nullable=False, default=0)
    updated_at = Column(Float, nullable=False)
    error_type = Column(String(80), nullable=False)


def profile_hash(voice: str, rate: str) -> str:
    return hashlib.sha256(f'{voice}\0{rate}'.encode()).hexdigest()


def _key(text: str, voice: str, rate: str) -> str:
    return hashlib.sha256(f'{get_content_hash(text)}\0{profile_hash(voice, rate)}'.encode()).hexdigest()


def ensure_schema(db: Session) -> None:
    """Additive, idempotent schema for existing installations, including workers."""
    engine = db.get_bind()
    with _schema_lock:
        if engine in _schema_engines:
            return
        with engine.begin() as connection:
            connection.execute(CreateTable(AudioRetry.__table__, if_not_exists=True))
            for index in AudioRetry.__table__.indexes:
                connection.execute(CreateIndex(index, if_not_exists=True))
        _schema_engines.add(engine)


def blocked_hashes(db: Session, voice: str, rate: str):
    ensure_schema(db)
    return select(AudioRetry.content_hash).where(
        AudioRetry.profile == profile_hash(voice, rate),
        AudioRetry.retry_at > time.time(),
    )


def retry_after(text: str, voice: str, rate: str) -> int:
    with get_db_session() as db:
        ensure_schema(db)
        row = db.get(AudioRetry, _key(text, voice, rate))
        return max(0, math.ceil(row.retry_at - time.time())) if row else 0


def record_failure(text: str, voice: str, rate: str, error_type: str,
                   *, db: Session | None = None) -> int:
    if db is None:
        with get_db_session() as session:
            return record_failure(text, voice, rate, error_type, db=session)
    ensure_schema(db)
    now = time.time()
    db.query(AudioRetry).filter(AudioRetry.updated_at < now - RETENTION_SECONDS).delete()
    key = _key(text, voice, rate)
    row = db.get(AudioRetry, key)
    if row is None:
        row = AudioRetry(key=key, content_hash=get_content_hash(text),
                         profile=profile_hash(voice, rate), failures=0)
        db.add(row)
    row.failures = min(row.failures + 1, 20)
    delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** min(row.failures - 1, 4))
    row.retry_at, row.updated_at = now + delay, now
    row.error_type = error_type[:80]
    db.query(StoryChapter).filter(StoryChapter.content_hash == row.content_hash,
                                StoryChapter.status != 'ready').update({'status': 'failed'})
    db.commit()
    return delay


def clear_failure(text: str, voice: str, rate: str) -> None:
    with get_db_session() as db:
        ensure_schema(db)
        db.query(AudioRetry).filter(AudioRetry.key == _key(text, voice, rate)).delete()
        db.query(StoryChapter).filter(StoryChapter.content_hash == get_content_hash(text)).update(
            {'status': 'ready', 'updated_at': time.time()})
        db.commit()
