"""Durable SSE snapshots use SQLite state and never load source prose."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.database import Base, StoryChapter
from services import audio_retry as retry, pregen_snapshot as snapshots


def test_snapshot_scopes_failures_and_completed_audio_to_story_and_voice(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(snapshots, 'get_db_session', Session)
    with Session() as db:
        db.add_all([
            StoryChapter(story_id='one', chapter_file='01.txt', chapter_num=0, status='pending', content_hash=retry.get_content_hash('synthetic one')),
            StoryChapter(story_id='two', chapter_file='01.txt', chapter_num=0, status='pending', content_hash=retry.get_content_hash('synthetic two')),
            StoryChapter(story_id='one', chapter_file='02.txt', chapter_num=1, status='ready'),
        ])
        db.commit()
        retry.record_failure('synthetic one', 'vi-VN-NamMinhNeural', '+20%', 'TimeoutError', db=db)
        retry.record_failure('synthetic two', 'different-voice', '+20%', 'TimeoutError', db=db)
    failures = snapshots.failure_snapshot('one')
    assert len(failures) == 1
    assert failures[0]['chapter_file'] == '01.txt'
    assert failures[0]['retry_after'] > 0
    assert snapshots.failure_snapshot('two') == []
    assert snapshots.ready_snapshot('one') == ['02.txt']
    assert snapshots.ready_snapshot('two') == []
    assert 'synthetic one' not in str(failures)


async def test_sse_snapshot_error_releases_subscription(monkeypatch):
    import pytest
    from types import SimpleNamespace
    from fastapi import HTTPException
    import routes.stories as route
    from services.pregen_stream import PregenStreamManager
    stream = PregenStreamManager()
    monkeypatch.setattr(route, 'pregen_stream_manager', stream)
    monkeypatch.setattr(route._state, 'bg_scheduler', None)
    def broken(_):
        raise RuntimeError('Synthetic database unavailable')
    monkeypatch.setattr(snapshots, 'failure_snapshot', broken)
    with pytest.raises(HTTPException) as error:
        await route.pregen_stream(SimpleNamespace(), story_id='one', _=None)
    assert error.value.status_code == 503
    assert stream.subscriber_count == 0


async def test_sse_initial_snapshot_restores_ready_failed_and_active_state(monkeypatch):
    import json
    from types import SimpleNamespace
    import routes.stories as route
    from services.pregen_stream import PregenStreamManager
    stream = PregenStreamManager()
    monkeypatch.setattr(route, 'pregen_stream_manager', stream)
    monkeypatch.setattr(route._state, 'bg_scheduler', None)
    monkeypatch.setattr(snapshots, 'failure_snapshot', lambda _: [{'chapter_file': '01.txt', 'retry_at': 1000}])
    monkeypatch.setattr(snapshots, 'ready_snapshot', lambda _: ['02.txt'])
    stream.broadcast({'type': 'chapter_progress', 'story_id': 'one', 'chapter_file': '03.txt', 'completed_chunks': 2, 'total_chunks': 4})
    response = await route.pregen_stream(SimpleNamespace(), story_id='one', _=None)
    event = await anext(response.body_iterator)
    assert event['event'] == 'snapshot'
    state = json.loads(event['data'])
    assert state['ready'] == ['02.txt']
    assert state['failures'][0]['chapter_file'] == '01.txt'
    assert state['active'][0]['completed_chunks'] == 2
    await response.body_iterator.aclose()
    assert stream.subscriber_count == 0
