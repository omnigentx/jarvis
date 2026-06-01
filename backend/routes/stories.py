"""Stories routes: list, chapters, play, crawl status, progress, pregen-stream (SSE)."""
import asyncio
import os
import re
import json
import time
import shutil
import logging

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from core.auth import verify_api_key
from core.database import StoryProgress, get_db_session
from helpers.text_processing import clean_text_for_tts
from helpers.audio_cache import get_audio_cache_path
from services.shared_state import (
    tts_cache, library_manager,
)
from services.pregen_stream import pregen_stream_manager
import services.shared_state as _state

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/stories", tags=["stories"])


def _update_story_progress(story_title: str, chapter_file: str):
    """Update story progress for resume/continue feature."""
    try:
        db = get_db_session()
        chapter_num = 0
        match = re.match(r'(\d+)', chapter_file)
        if match:
            chapter_num = int(match.group(1))
        
        progress = db.query(StoryProgress).filter(
            StoryProgress.story_title == story_title,
        ).first()
        
        if progress:
            progress.last_chapter_num = chapter_num
            progress.last_chapter_file = chapter_file
            progress.last_played_at = time.time()
        else:
            progress = StoryProgress(
                story_title=story_title,
                last_chapter_num=chapter_num,
                last_chapter_file=chapter_file,
                last_played_at=time.time(),
            )
            db.add(progress)
        
        db.commit()
        logger.debug(f"Story progress updated: {story_title} -> chapter {chapter_num}")
    except Exception as e:
        logger.error(f"Failed to update story progress: {e}")
    finally:
        db.close()


@router.get("")
async def list_local_stories(_=Depends(verify_api_key)):
    """List all crawled/local stories with user progress.
    Primary source: story_meta DB table. Fallback: FS scan for unregistered stories."""
    from core.database import StoryMeta
    
    stories_dir = "data/stories"
    results = []
    
    # Load progress from DB
    progress_map = {}
    db = get_db_session()
    try:
        progresses = db.query(StoryProgress).all()
        for p in progresses:
            progress_map[p.story_title] = {
                "last_chapter_file": p.last_chapter_file,
                "last_chapter_num": p.last_chapter_num,
                "last_played_at": p.last_played_at,
            }
        
        # Query story_meta for registered stories
        registered = set()
        metas = db.query(StoryMeta).all()
        for m in metas:
            # Verify folder still exists
            story_path = os.path.join(stories_dir, m.story_id)
            if not os.path.isdir(story_path):
                continue
            
            # Re-count chapters from FS (authoritative)
            chapter_count = len([f for f in os.listdir(story_path) if f.endswith(".txt")])
            if chapter_count != m.chapter_count:
                m.chapter_count = chapter_count
            
            entry = {"id": m.story_id, "title": m.title, "chapters": chapter_count}
            
            prog = progress_map.get(m.story_id)
            if prog:
                entry["last_chapter_file"] = prog["last_chapter_file"]
                entry["last_chapter_num"] = prog["last_chapter_num"]
                entry["last_played_at"] = prog["last_played_at"]
            
            results.append(entry)
            registered.add(m.story_id)
        
        # Fallback: scan FS for stories not yet in DB (manually copied, etc.)
        if os.path.exists(stories_dir):
            for name in os.listdir(stories_dir):
                if name in registered or name.startswith('.'):
                    continue
                story_path = os.path.join(stories_dir, name)
                if not os.path.isdir(story_path):
                    continue
                
                chapter_count = len([f for f in os.listdir(story_path) if f.endswith(".txt")])
                if chapter_count == 0:
                    continue
                
                # Auto-register in DB
                new_meta = StoryMeta(
                    story_id=name,
                    title=name,
                    chapter_count=chapter_count,
                )
                db.add(new_meta)
                
                entry = {"id": name, "title": name, "chapters": chapter_count}
                prog = progress_map.get(name)
                if prog:
                    entry["last_chapter_file"] = prog["last_chapter_file"]
                    entry["last_chapter_num"] = prog["last_chapter_num"]
                    entry["last_played_at"] = prog["last_played_at"]
                results.append(entry)
        
        db.commit()
    except Exception as e:
        logger.error(f"Error listing stories: {e}")
        db.rollback()
        return []
    finally:
        db.close()
    
    results.sort(key=lambda x: (-(x.get("last_played_at") or 0), x["title"]))
    return results


@router.delete("/{story_id}")
async def delete_story_endpoint(story_id: str, _=Depends(verify_api_key)):
    """Delete a story and ALL associated data (cascade cleanup).
    
    Cleans up:
    - Audio cache entries + MP3 files (via audio_cache module)
    - Story metadata (story_meta table)
    - Story progress (story_progress table)
    - Library book entries (books table) with url prefix 'local://{story_id}/'
    - TTS cache entries (tts_cache table) with key prefix 'story_{story_id}_'
    - Story text files (data/stories/{story_id}/ directory)
    """
    from core.database import StoryMeta, Book, TTSCache, CrawlJob
    from helpers.audio_cache import delete_audio_for_story
    
    stories_dir = "data/stories"
    path = os.path.join(stories_dir, story_id)
    
    if not os.path.exists(path):
        return JSONResponse(status_code=404, content={"error": "Story not found"})
    
    cleanup_report = {
        "audio_files": 0,
        "db_records": 0,
    }
    
    try:
        # 1. Delete audio cache entries + files (DB + disk)
        cleanup_report["audio_files"] = delete_audio_for_story(story_id)
        
        # Also clean audio by reading chapter text hashes (for entries without story_id tag)
        try:
            files = [f for f in os.listdir(path) if f.endswith(".txt")]
            for filename in files:
                try:
                    file_path = os.path.join(path, filename)
                    with open(file_path, "r", encoding="utf-8") as f:
                        raw_text = f.read()
                    text = clean_text_for_tts(raw_text)
                    if text:
                        cache_path = get_audio_cache_path(text)
                        for suffix in ["", ".part", ".lock", ".part.json"]:
                            p = cache_path + suffix if suffix else cache_path
                            if os.path.exists(p):
                                os.remove(p)
                                cleanup_report["audio_files"] += 1
                except Exception as e:
                    logger.error(f"Error cleaning audio for {filename}: {e}")
        except Exception as e:
            logger.error(f"Error scanning chapters for audio cleanup: {e}")
        
        # 2. Cascade delete from all related DB tables
        db = get_db_session()
        try:
            # story_meta
            db.query(StoryMeta).filter(StoryMeta.story_id == story_id).delete()
            
            # story_progress
            db.query(StoryProgress).filter(StoryProgress.story_title == story_id).delete()
            
            # books (library entries) — match by url prefix or title
            deleted_books = db.query(Book).filter(
                Book.url.like(f"local://{story_id}/%")
            ).delete(synchronize_session='fetch')
            
            # Also delete books matching by title
            deleted_books += db.query(Book).filter(
                Book.title == story_id
            ).delete(synchronize_session='fetch')
            
            # tts_cache entries — match by request_id prefix
            deleted_tts = db.query(TTSCache).filter(
                TTSCache.request_id.like(f"story_{story_id}_%")
            ).delete(synchronize_session='fetch')
            
            # crawl_jobs — clean up any associated crawl jobs
            db.query(CrawlJob).filter(CrawlJob.story_title == story_id).delete()
            
            db.commit()
            cleanup_report["db_records"] = deleted_books + deleted_tts
            logger.info(f"Cascade DB cleanup for '{story_id}': "
                       f"books={deleted_books}, tts_cache={deleted_tts}")
        finally:
            db.close()
        
        # 3. Delete story directory (text files)
        shutil.rmtree(path)
        logger.info(f"Deleted story directory: {path}")
        
        return {
            "status": "deleted",
            "audio_files_cleaned": cleanup_report["audio_files"],
            "db_records_cleaned": cleanup_report["db_records"],
        }
    
    except Exception as e:
        logger.error(f"Delete failed: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        # Trigger TTS pregen rescan
        if _state.bg_scheduler:
            for job in _state.bg_scheduler._jobs:
                if hasattr(job, 'rescan'):
                    job.rescan()


@router.get("/crawl/status")
async def get_all_crawl_status(_=Depends(verify_api_key)):
    """Return all active crawl jobs for the background activity panel."""
    try:
        from core.database import CrawlJob
        db = get_db_session()
        try:
            jobs = db.query(CrawlJob).filter(
                CrawlJob.status.in_(["running", "pending"])
            ).all()
            active_jobs = [{
                "job_id": j.job_id,
                "story_title": j.story_title or "Unknown",
                "current_chapter": j.current_chapter or 0,
                "total_chapters": j.total_chapters or 0,
                "message": j.message or "",
                "status": j.status,
            } for j in jobs]
            return {"active_jobs": active_jobs}
        finally:
            db.close()
    except Exception as e:
        return {"active_jobs": [], "error": str(e)}


@router.get("/crawl/status/{job_id}")
async def get_crawl_job_status(job_id: str, _=Depends(verify_api_key)):
    """Check status of a background crawl job."""
    try:
        from tools.story_server import get_crawl_status
        result_json = get_crawl_status(job_id)
        return json.loads(result_json)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/crawl/cancel/{job_id}")
async def cancel_crawl_job(job_id: str, _=Depends(verify_api_key)):
    """Cancel a background crawl job."""
    try:
        from core.database import CrawlJob
        db = get_db_session()
        try:
            job = db.query(CrawlJob).filter(CrawlJob.job_id == job_id).first()
            if job:
                job.status = "cancelled"
                job.message = "Cancelled by user."
                db.commit()
                return {"status": "cancelled", "job_id": job_id}
            return {"status": "not_found", "job_id": job_id}
        finally:
            db.close()
    except Exception as e:
        return {"status": "error", "message": str(e)}



@router.get("/{story_id}/chapters")
async def list_story_chapters(story_id: str, _=Depends(verify_api_key)):
    stories_dir = "data/stories"
    path = os.path.join(stories_dir, story_id)
    if not os.path.exists(path): return []
    
    try:
        files = sorted([f for f in os.listdir(path) if f.endswith(".txt")])
        
        # Get the SINGLE currently-generating chapter from scheduler (source of truth)
        active_chapter = None
        if _state.bg_scheduler:
            active = _state.bg_scheduler.get_active_pregen_chapter()
            if active and active.get("story_title") == story_id:
                active_chapter = active.get("chapter_file")
        
        result = []
        for f in files:
            try:
                with open(os.path.join(path, f), "r", encoding="utf-8") as fh:
                    text = clean_text_for_tts(fh.read())
                
                cache_path = get_audio_cache_path(text)
                
                if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                    preload = "ready"
                elif f == active_chapter:
                    preload = "generating"
                else:
                    preload = "none"
            except:
                preload = "none"
            result.append({"file": f, "preload": preload})
        return result
    except Exception as e:
        logger.error(f"Error listing chapters for {story_id}: {e}")
        return []


@router.get("/{story_id}/chapters/{filename}")
async def get_chapter_text(story_id: str, filename: str, _=Depends(verify_api_key)):
    path = os.path.join("data/stories", story_id, filename)
    if not os.path.exists(path): 
        return {"error": "Not found", "content": "Chapter not found."}
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"content": content}
    except Exception as e:
        return {"error": str(e)}


@router.post("/{story_id}/{filename}/play")
async def play_local_chapter(story_id: str, filename: str, _=Depends(verify_api_key)):
    path = os.path.join("data/stories", story_id, filename)
    if not os.path.exists(path): return {"error": "Not found"}
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        
        text = clean_text_for_tts(text)
        safe_fname = re.sub(r'[^\w\-\.]', '_', filename)
        unique_id = f"story_{story_id}_{safe_fname}"
        
        cache_path = get_audio_cache_path(text)
        mp3_exists = os.path.exists(cache_path)
        
        title = "Story"
        chapter = filename.replace(".txt", "")
        if "_" in filename:
             parts = filename.split("_", 1)
             if len(parts) > 1: chapter = parts[1].replace(".txt", "").replace("_", " ")

        library_manager.add_book(title, chapter, url=f"local://{story_id}/{filename}", 
                                 id_override=unique_id)
        
        tts_cache.save_tts_text(unique_id, text)
        _update_story_progress(story_id, filename)
        
        response = {"audio_url": f"/api/tts/{unique_id}"}
        
        if mp3_exists:
            try:
                from mutagen.mp3 import MP3
                audio = MP3(cache_path)
                duration = int(audio.info.length)
                library_manager.set_status(unique_id, "ready", duration=duration)
                response["duration"] = duration
                response["status"] = "ready"
                logger.debug(f"Pre-gen play: {filename}, duration={duration}s")
            except Exception as e:
                library_manager.set_status(unique_id, "ready")
                logger.warning(f"Could not extract duration for {filename}: {e}")
        else:
            if _state.bg_scheduler and _state.bg_scheduler.is_running():
                _state.bg_scheduler.notify_tts_activity()
                # Only cancel pre-gen if it's working a DIFFERENT chapter.
                # Cancelling the chapter the user just clicked would delete its
                # half-written mp3 + lock mid-stream → the live stream closes at
                # ~11s and the player auto-advances. The .lock on this exact
                # cache_path means pre-gen already owns it → hand over instead.
                if not os.path.exists(cache_path + ".lock"):
                    _state.bg_scheduler.request_cancel()
        
        return response
    except Exception as e:
        logger.error(f"Play error: {e}")
        return {"error": str(e)}


@router.get("/{story_id}/progress")
async def get_story_progress(story_id: str, _=Depends(verify_api_key)):
    """Get last chapter progress for a story."""
    try:
        db = get_db_session()
        progress = db.query(StoryProgress).filter(
            StoryProgress.story_title == story_id,
        ).first()
        db.close()
        
        if progress:
            return {
                "story_title": progress.story_title,
                "last_chapter_num": progress.last_chapter_num,
                "last_chapter_file": progress.last_chapter_file,
                "last_played_at": progress.last_played_at,
            }
        return {"last_chapter_num": 0, "last_chapter_file": None}
    except Exception as e:
        return {"error": str(e)}


@router.get("/pregen-stream")
async def pregen_stream(
    request: Request,
    story_id: str = Query(None, description="Filter events to this story only"),
    _=Depends(verify_api_key),
):
    """SSE stream for TTS pre-generation status.
    
    Events:
      - queue_update: full queue snapshot (on connect + when queue changes)
      - chapter_generating: a chapter started generating
      - chapter_ready: a chapter finished generating
      - chapter_error: generation failed
      - scheduler_idle: no more tasks
    """
    sub_id, queue = pregen_stream_manager.subscribe(story_id=story_id)
    
    # Send initial queue snapshot
    initial_queue = []
    if _state.bg_scheduler:
        for job in _state.bg_scheduler._jobs:
            if hasattr(job, 'preview_queue'):
                initial_queue = job.preview_queue(story_id=story_id, limit=10)
                break
    
    async def event_generator():
        try:
            # Initial snapshot
            yield {
                "event": "queue_update",
                "data": json.dumps({"type": "queue_update", "queue": initial_queue}),
            }
            
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {
                        "event": event.get("type", "message"),
                        "data": json.dumps(event),
                    }
                except asyncio.TimeoutError:
                    # Send keepalive ping
                    yield {"event": "ping", "data": ""}
        finally:
            pregen_stream_manager.unsubscribe(sub_id)
    
    return EventSourceResponse(event_generator())
