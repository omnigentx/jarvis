"""Queue preparation must not pretend the next chapter has been listened to."""
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from fastapi import HTTPException

@pytest.fixture
def playback(tmp_path, monkeypatch):
    import services.story_playback as service
    from services import shared_state as state
    import routes.stories as route
    monkeypatch.chdir(tmp_path)
    source = tmp_path / 'data/stories/synthetic'
    source.mkdir(parents=True)
    (source/'01.txt').write_text('Synthetic acceptance chapter. '*400)
    monkeypatch.setattr(state,'library_manager',MagicMock())
    monkeypatch.setattr(state,'tts_cache',MagicMock())
    monkeypatch.setattr(state,'bg_scheduler',MagicMock())
    monkeypatch.setattr(route,'_update_story_progress',MagicMock())
    return service,state,route

@pytest.mark.asyncio
async def test_prepare_registers_source_without_progress_or_scheduler_side_effects(playback):
    service,state,route=playback
    result=await route.prepare_local_chapter('synthetic','01.txt')
    assert result['audio_url']=='/api/tts/story_synthetic_01.txt'
    assert result['status']=='none'
    state.tts_cache.save_tts_text.assert_called_once()
    state.library_manager.add_book.assert_called_once()
    route._update_story_progress.assert_not_called()
    assert not state.bg_scheduler.mock_calls

@pytest.mark.asyncio
async def test_play_still_records_selected_chapter(playback):
    _,state,route=playback
    await route.play_local_chapter('synthetic','01.txt')
    route._update_story_progress.assert_called_once_with('synthetic','01.txt')
    state.bg_scheduler.notify_tts_activity.assert_called_once()

@pytest.mark.parametrize('story,file',[('..','01.txt'),('synthetic','../01.txt'),('synthetic','missing.txt')])
def test_prepare_rejects_missing_or_escaping_source(playback,story,file):
    service,_,_=playback
    with pytest.raises(HTTPException) as exc:service.prepare_story_source(story,file)
    assert exc.value.status_code==404


def test_prepare_rejects_symlink_escape(playback,tmp_path):
    service,_,_=playback
    outside=tmp_path/'outside.txt';outside.write_text('Synthetic outside scope')
    Path('data/stories/synthetic/link.txt').symlink_to(outside)
    with pytest.raises(HTTPException):service.prepare_story_source('synthetic','link.txt')


def test_empty_chapter_does_not_register_audio(playback):
    service,state,_=playback
    Path('data/stories/synthetic/empty.txt').write_text('')
    with pytest.raises(HTTPException) as exc:service.prepare_story_source('synthetic','empty.txt')
    assert exc.value.status_code==422
    state.tts_cache.save_tts_text.assert_not_called()


def test_legacy_growing_cache_is_not_ready(playback,monkeypatch):
    service,_,_=playback
    Path('growing.mp3').write_bytes(b'A'*100000)
    Path('growing.mp3.lock').touch()
    monkeypatch.setattr(service,'get_audio_cache_path',lambda _: 'growing.mp3')
    assert service.prepare_story_source('synthetic','01.txt')['status']=='none'


def test_completed_valid_cache_returns_ready_metadata(playback, monkeypatch):
    service, state, _ = playback
    import mutagen.mp3
    Path('complete.mp3').write_bytes(b'A' * 100000)
    monkeypatch.setattr(service, 'get_audio_cache_path', lambda _: 'complete.mp3')
    monkeypatch.setattr(mutagen.mp3, 'MP3', lambda _: MagicMock(info=MagicMock(length=12.5)))
    result = service.prepare_story_source('synthetic', '01.txt')
    assert result['status'] == 'ready'
    assert result['duration'] == 12.5
    state.library_manager.set_status.assert_called_once_with('story_synthetic_01.txt', 'ready', duration=12)


def test_invalid_cache_does_not_claim_ready(playback, monkeypatch):
    service, state, _ = playback
    Path('invalid.mp3').write_bytes(b'A' * 100000)
    monkeypatch.setattr(service, 'get_audio_cache_path', lambda _: 'invalid.mp3')
    result = service.prepare_story_source('synthetic', '01.txt')
    assert result['status'] == 'none'
    assert 'duration' not in result
    state.library_manager.set_status.assert_not_called()
