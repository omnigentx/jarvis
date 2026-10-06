"""Persisted bounded cooldowns, additive upgrades and changed speech profiles."""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from core.database import Base
from services import audio_retry as retry


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "retry.db"}')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(retry, 'get_db_session', Session)
    return Session


def test_exponential_cooldown_is_bounded_and_reset_after_success(db, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(retry.time, 'time', lambda: now)
    delays = [retry.record_failure('Synthetic', 'voice', '+20%', 'TimeoutError') for _ in range(8)]
    assert delays == [60, 120, 240, 480, 900, 900, 900, 900]
    assert retry.retry_after('Synthetic', 'voice', '+20%') == 900
    assert retry.retry_after('Synthetic', 'other voice', '+20%') == 0
    assert retry.retry_after('Synthetic', 'voice', '+0%') == 0
    assert retry.retry_after('changed text', 'voice', '+20%') == 0
    now += 901
    assert retry.retry_after('Synthetic', 'voice', '+20%') == 0
    retry.clear_failure('Synthetic', 'voice', '+20%')
    assert retry.record_failure('Synthetic', 'voice', '+20%', 'TimeoutError') == 60


def test_schema_upgrade_is_additive_and_retry_survives_new_engine(db):
    with db() as session:
        bind = session.get_bind()
        retry.AudioRetry.__table__.drop(bind)
    retry.record_failure('Synthetic', 'voice', '+20%', 'TimeoutError')
    reopened = sessionmaker(bind=create_engine(str(bind.url)))
    with reopened() as session:
        retry.ensure_schema(session)
        assert session.query(retry.AudioRetry).one().failures == 1
        assert session.execute(text('select count(*) from story_chapters')).scalar() == 0


def test_expired_retry_rows_are_pruned(db, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(retry.time, 'time', lambda: now)
    retry.record_failure('old synthetic', 'voice', '+20%', 'TimeoutError')
    now += retry.RETENTION_SECONDS + 1
    retry.record_failure('new synthetic', 'voice', '+20%', 'TimeoutError')
    with db() as session:
        assert session.query(retry.AudioRetry).count() == 1
