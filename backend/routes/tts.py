"""TTS routes: cancel, stream/serve audio."""
import os
import re
import uuid
import asyncio
import logging

import aiofiles
from fastapi import APIRouter, Request, Depends
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel

from core.auth import verify_api_key, verify_optional_api_key
from helpers.text_processing import clean_text_for_tts
from helpers.audio_cache import get_audio_cache_path
from services.shared_state import (
    tts_cache, library_manager, tts_provider, generation_tasks,
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
async def tts_endpoint(request_id: str, request: Request, _auth=Depends(verify_optional_api_key)):
    # Notify scheduler: on-demand TTS activity
    if _state.bg_scheduler:
        _state.bg_scheduler.notify_tts_activity()
        if _state.bg_scheduler.is_generating(request_id):
            logger.debug(f"[KB1] Handover: pre-gen working on {request_id}, streaming in progress")
        elif _state.bg_scheduler.is_running():
            logger.debug(f"[KB2] Cancel pre-gen: user requested {request_id}")
            _state.bg_scheduler.request_cancel()
    
    # Check if this request_id belongs to the Library
    book = library_manager.get_book(request_id)
    text = None
    cache_path = None
    
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

    # Check file states
    mp3_exists = os.path.exists(cache_path)
    part_path = cache_path + ".part"
    part_exists = os.path.exists(part_path)
    
    if not text and not mp3_exists and not part_exists:
         return {"error": "Text missing for generation"}

    # Handle HEAD request
    if request.method == "HEAD":
        file_size = 0
        if mp3_exists:
             file_size = os.path.getsize(cache_path)
        
        headers = {"Accept-Ranges": "bytes", "Content-Type": "audio/mpeg"}
        if file_size > 0:
            headers["Content-Length"] = str(file_size)
        return Response(status_code=200, headers=headers)

    # Check/Start Background Generation
    lock_path = cache_path + ".lock"
    meta_path = cache_path + ".part.json" 
    
    is_generating = False
    if os.path.exists(lock_path):
        if request_id in generation_tasks:
            logger.debug(f"Task {request_id} already running (internal).")
            is_generating = True
        else:
            logger.debug(f"Found lock file but no task (Interrupted/Stale): {lock_path}")
            is_generating = True
    
    if mp3_exists:
        if text:
            file_size = os.path.getsize(cache_path)
            expected_min = len(text) * 3
            if file_size < expected_min:
                logger.warning(f"Truncated file detected: {file_size}B < min {expected_min}B. Removing.")
                os.remove(cache_path)
                mp3_exists = False

    if mp3_exists:
        if book and book.status != "ready":
            try:
                from mutagen.mp3 import MP3
                audio = MP3(cache_path)
                duration = int(audio.info.length)
                library_manager.set_status(book.id, "ready", duration=duration)
                logger.debug(f"Pre-gen ready: {request_id}, duration={duration}s")
            except Exception as e:
                library_manager.set_status(book.id, "ready")
                logger.warning(f"Could not extract duration: {e}")
        
    elif is_generating or not mp3_exists:
         if request_id not in generation_tasks:
             if book: library_manager.set_status(book.id, "generating")
             logger.debug(f"Starting/Resuming generation for {request_id}...")
             
             async def generate_worker(start_idx=0):
                 task_id = request_id
                 generation_tasks[task_id] = asyncio.current_task()
                 
                 try:
                     with open(lock_path, 'w') as lf: lf.write("locked")
                 except: pass

                 try:
                     async with aiofiles.open(cache_path, "wb") as f:
                         async for chunk in tts_provider.stream_audio(text):
                             if chunk:
                                 await f.write(chunk)
                                 await f.flush()

                     logger.debug(f"Generation Complete for {cache_path}")
                     if os.path.exists(lock_path): os.remove(lock_path)
                     if os.path.exists(meta_path): os.remove(meta_path)
                     if book: library_manager.set_status(book.id, "ready")

                 except asyncio.CancelledError:
                     logger.debug(f"Generation task {task_id} cancelled.")
                 except Exception as e:
                     logger.error(f"Generation task {task_id} failed: {e}")
                     if os.path.exists(cache_path):
                         try:
                             os.remove(cache_path)
                             logger.debug(f"Removed partial file: {cache_path}")
                         except Exception as rm_err:
                             logger.error(f"Failed to remove partial: {rm_err}")
                 finally:
                     if task_id in generation_tasks: del generation_tasks[task_id]
                     if os.path.exists(lock_path): os.remove(lock_path)

             asyncio.create_task(generate_worker())

    # Stream appropriate file
    target_file = cache_path

    # Give the newly-created task a chance to start and write the lock file.
    # Without this yield, is_generating / is_live_mode checks below fire before
    # the coroutine runs even one iteration, giving is_live_mode=False + file_size=0.
    await asyncio.sleep(0)  # yield to event loop so generate_worker can start

    # Wait for lock to appear (up to 1s) before deciding live vs static mode
    for _ in range(10):
        if os.path.exists(lock_path) or os.path.exists(target_file):
            break
        await asyncio.sleep(0.1)

    # For notification TTS (neither story_ nor library book), wait for full generation
    # to complete before serving. This prevents the browser from firing 'ended' early
    # due to gaps between EdgeTTS chunks during live streaming.
    # Story/library requests keep live streaming for fast TTFB (they can be very large).
    is_notification = not book and not (request_id.startswith('story_'))
    if is_notification and os.path.exists(lock_path):
        logger.debug(f"[TTS] Waiting for full generation before serving notification TTS: {request_id}")
        for _ in range(300):  # up to 30s
            if not os.path.exists(lock_path):
                break
            await asyncio.sleep(0.1)
        logger.debug(f"[TTS] Generation done, serving static: {request_id}")

    for _ in range(50):
        if os.path.exists(target_file): break
        await asyncio.sleep(0.1)
        
    if not os.path.exists(target_file):
        return {"error": "File not created yet"}

    async def file_tailer(offset=0):
        try:
             async with aiofiles.open(target_file, "rb") as f:
                 if offset > 0: await f.seek(offset)
                 
                 while True:
                     chunk = await f.read(8192)
                     if chunk:
                         yield chunk
                     else:
                         is_still_generating = os.path.exists(lock_path)
                         if not is_still_generating:
                             break
                         await asyncio.sleep(0.1)
             if _state.bg_scheduler:
                  _state.bg_scheduler.request_resume()
                  _state.bg_scheduler.notify_tts_done()
        except Exception as e:
            logger.error(f"Stream error: {e}")

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": "audio/mpeg",
        "Cache-Control": "no-cache, no-store, must-revalidate"
    }

    status_code = 200
    file_size = 0
    is_live_mode = os.path.exists(lock_path)
    
    if is_live_mode:
        status_code = 200
        logger.debug("Serving Live Stream (Lock exists)")
    else:
        try:
            file_size = os.path.getsize(cache_path)
            logger.debug(f"Serving Method: MP3 (Static). Size: {file_size}")
        except:
             logger.error("MP3 size check failed")
             status_code = 200

    start_byte = 0
    end_byte = None
    length_to_serve = None
    
    range_header = request.headers.get('Range')
    if range_header:
        if not is_live_mode:
            status_code = 206
            try:
                logger.debug(f"Received Range Header: {range_header}")
                range_match = re.search(r'bytes=(\d+)-(\d*)', range_header, re.IGNORECASE)
                if not range_match:
                     raise ValueError("Invalid Range Format")
                
                start_byte = int(range_match.group(1))
                end_byte = int(range_match.group(2)) if range_match.group(2) else None
                
                if start_byte >= file_size:
                    logger.warning(f"Range Request Out of Bounds: {start_byte} >= {file_size}")
                    return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})

                actual_end = end_byte if end_byte is not None else file_size - 1
                if actual_end >= file_size: actual_end = file_size - 1
                
                length_to_serve = actual_end - start_byte + 1
                
                headers["Content-Range"] = f"bytes {start_byte}-{actual_end}/{file_size}"
                headers["Content-Length"] = str(length_to_serve)
                logger.debug(f"Handling Range Request: {headers['Content-Range']} (Size: {length_to_serve})")
            except Exception as e:
                logger.error(f"Failed to parse range header '{range_header}': {e}")
                status_code = 200
                pass 
        else:
             status_code = 200
             logger.debug("Ignoring Range header for Active Stream (Live Mode)")
    else:
        if not is_live_mode:
            headers["Content-Length"] = str(file_size)
            logger.debug("Serving Full MP3 (200 OK)")

    if is_live_mode:
        return StreamingResponse(
            file_tailer(0),
            status_code=200,
            headers=headers,
            media_type="audio/mpeg"
        )
    else:
        async def limited_stream():
            count = 0
            async for chunk in file_tailer(start_byte):
                if length_to_serve:
                    if count + len(chunk) > length_to_serve:
                        yield chunk[:length_to_serve - count]
                        break
                yield chunk
                count += len(chunk)
                if length_to_serve and count >= length_to_serve: break
        
        return StreamingResponse(
            limited_stream(),
            status_code=status_code,
            headers=headers,
            media_type="audio/mpeg"
        )
