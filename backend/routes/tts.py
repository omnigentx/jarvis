"""TTS routes: cancel, stream/serve audio."""
import os
import re
import uuid
import asyncio
import logging

from fastapi import APIRouter, Request, Depends
from fastapi.responses import Response
from pydantic import BaseModel

from core.auth import verify_api_key
from helpers.text_processing import clean_text_for_tts
from helpers.audio_cache import get_audio_cache_path
from services.shared_state import (
    tts_cache, library_manager, generation_tasks,
)
import services.shared_state as _state

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tts", tags=["tts"])


class TTSPrepareRequest(BaseModel):
    text: str


@router.post("/prepare")
async def prepare_tts(body: TTSPrepareRequest, _=Depends(verify_api_key)):
    """Register arbitrary text in TTS cache and return a streamable request_id.

    Flow:
      1. Client POSTs {text: "..."} → gets back {request_id, audio_url}
      2. Client sets <audio src=audio_url> → GET /api/tts/{request_id} streams MP3
         (generation starts on first GET, just like chat TTS)
    """
    if not body.text or not body.text.strip():
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"detail": "text must not be empty"})

    cleaned = clean_text_for_tts(body.text.strip())
    request_id = str(uuid.uuid4())
    tts_cache.save_tts_text(request_id, cleaned)
    audio_url = f"/api/tts/{request_id}"
    logger.info(f"[TTS] /prepare request_id={request_id} text_len={len(cleaned)}")
    return {"request_id": request_id, "audio_url": audio_url}


@router.post("/cancel/{request_id}")
async def cancel_tts(request_id: str, _=Depends(verify_api_key)):
    """Cancel an ongoing TTS generation task."""
    task = generation_tasks.get(request_id)
    if task:
        task.cancel()
        logger.debug(f"User requested cancellation for task {request_id}")
        return {"status": "cancelled", "message": f"Task {request_id} cancelled."}
    return {"status": "not_found", "message": "No active task found."}


@router.api_route("/{request_id}", methods=["GET", "HEAD"])
async def tts_endpoint(request_id: str, request: Request, _auth=Depends(verify_api_key)):
    # Notify scheduler of on-demand activity. The cancel-vs-handover decision
    # is DEFERRED until we've resolved cache_path + lock state below. The old
    # is_generating(request_id) check never matched — pre-gen keys its tasks by
    # {story_title, chapter_file}, never request_id — so it ALWAYS fell through
    # to request_cancel(), cancelling the very chapter the user just asked for.
    # That deleted the half-written mp3 + lock mid-stream → the live stream
    # closed at ~11s → the browser fired 'ended' → auto-advanced. The lock file
    # on THIS cache_path (computed below) is the real source of truth for
    # "is this exact audio already being generated".
    if _state.bg_scheduler:
        _state.bg_scheduler.notify_tts_activity()
    
    # Check if this request_id belongs to the Library
    book = library_manager.get_book(request_id)
    text = None
    cache_path = None
    # is_notification = chat audio + cron — uses the registry-driven chat
    # provider. Books / stories use the locked Edge stories provider.
    is_notification = not book and not request_id.startswith('story_')
    
    if book:
        logger.debug(f"Streaming from Library: {book.title}")
        text = tts_cache.get_tts_text(request_id)
        
        if text:
            cache_path = get_audio_cache_path(text)
            if book.file_path != cache_path:
                library_manager.update_file_path(request_id, cache_path)
        else:
            cache_path = book.file_path
        
        if not text and not os.path.exists(cache_path) and not os.path.exists(cache_path + ".part"):
            return {"error": "Book source missing and text not available."}
            
    else:
        text = tts_cache.get_tts_text(request_id)
        
        # Self-heal: recover text for Local Stories if missing
        if not text and request_id.startswith("story_"):
            try:
                prefix_removed = request_id[6:]
                stories_dir = "data/stories"
                if os.path.exists(stories_dir):
                    for s_id in os.listdir(stories_dir):
                        if prefix_removed.startswith(s_id + "_"):
                            rem_len = len(s_id) + 1
                            safe_fname = prefix_removed[rem_len:]
                            s_path = os.path.join(stories_dir, s_id)
                            candidate_file = None
                            for f in os.listdir(s_path):
                                if f.endswith(".txt"):
                                    sf = re.sub(r'[^\w\-\.]', '_', f)
                                    if sf == safe_fname:
                                        candidate_file = f
                                        break
                            
                            if candidate_file:
                                logger.debug(f"Recovering text for {request_id} from {candidate_file}")
                                with open(os.path.join(s_path, candidate_file), "r", encoding="utf-8") as f:
                                    text = clean_text_for_tts(f.read())
                                    tts_cache.save_tts_text(request_id, text)
                                break
            except Exception as e:
                logger.error(f"Self-heal failed: {e}")

        if not text:
            return {"error": "Text not found or expired"}
        cache_path = get_audio_cache_path(text)

    from services.tts_audio_response import audio_response
    provider = _state.tts_chat_provider if is_notification else _state.tts_stories_provider
    return await audio_response(
        cache_path, text, request_id, request, provider, generation_tasks,
        library_manager, book, _state.bg_scheduler, wait_complete=is_notification,
    )


@router.get('/{request_id}/status-stream')
async def audio_status_stream(request_id: str, _=Depends(verify_api_key)):
    """Snapshot + pushed completion for underrun recovery; never poll HEAD."""
    import json
    from sse_starlette.sse import EventSourceResponse
    from services.audio_generation import active_generation
    from fastapi import HTTPException

    text = tts_cache.get_tts_text(request_id)
    if not text:
        raise HTTPException(404, 'Audio source is unavailable')
    path = get_audio_cache_path(text)
    generation = active_generation(path)

    async def events():
        while True:
            revision = generation.revision if generation else 0
            if generation and not generation.done:
                status = 'generating'
            elif generation and generation.error:
                status = 'error'
            else:
                status = 'ready' if os.path.isfile(path) else 'error'
            yield {'event': 'status', 'data': json.dumps({'status': status})}
            if status != 'generating':
                return
            try:
                await generation.wait(revision)
            except asyncio.TimeoutError:
                yield {'event': 'status', 'data': json.dumps({'status': 'error'})}
                return

    return EventSourceResponse(events())
