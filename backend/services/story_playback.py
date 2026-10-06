"""Prepare one story source without starting playback or changing progress."""
from pathlib import Path
import re
import logging

from fastapi import HTTPException
from helpers.audio_cache import get_audio_cache_path
from helpers.text_processing import clean_text_for_tts
from services.audio_generation import active_generation
from services import shared_state as state


logger = logging.getLogger(__name__)

def prepare_story_source(story_id: str, filename: str) -> dict:
    root = Path('data/stories').resolve()
    directory = (root / story_id).resolve()
    source = (directory / filename).resolve()
    if (directory.parent != root or source.parent != directory
            or source.suffix != '.txt' or not source.is_file()):
        raise HTTPException(404, 'Chapter not found')
    text = clean_text_for_tts(source.read_text(encoding='utf-8'))
    if not text:
        raise HTTPException(422, 'Chapter contains no readable text')
    safe_name = re.sub(r'[^\w\-\.]', '_', filename)
    request_id = f'story_{story_id}_{safe_name}'
    chapter = filename.removesuffix('.txt').split('_', 1)[-1].replace('_', ' ')
    state.library_manager.add_book('Story', chapter, url=f'local://{story_id}/{filename}', id_override=request_id)
    state.tts_cache.save_tts_text(request_id, text)
    cache = Path(get_audio_cache_path(text))
    result = {'audio_url': f'/api/tts/{request_id}', 'status': 'none'}
    # A legacy growing MP3 is not a complete chapter, even when it exists.
    if (cache.is_file() and cache.stat().st_size >= len(text) * 3
            and not active_generation(str(cache)) and not Path(str(cache) + '.lock').exists()):
        from mutagen.mp3 import MP3
        try:
            result['duration'] = MP3(cache).info.length
            result['status'] = 'ready'
            state.library_manager.set_status(request_id, 'ready', duration=int(result['duration']))
        except Exception:
            result.pop('duration', None)
            result['status'] = 'none'
            logger.warning('[STORY] Cached audio validation failed; generation required', exc_info=True)
    return result
