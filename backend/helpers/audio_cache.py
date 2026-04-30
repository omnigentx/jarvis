"""Audio cache management — single authority for audio file lifecycle.

All audio cache operations go through this module:
- Path generation (get_audio_cache_path)
- Registration (register_audio) 
- Status transitions (mark_ready, mark_failed)
- Eviction (clean_audio_cache)
- Stats (get_cache_stats)
- Startup cleanup (cleanup_stale_generating)
"""
import os
import time
import hashlib
import logging

logger = logging.getLogger(__name__)

AUDIO_CACHE_DIR = os.path.join("data", "audio_cache")
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)


def get_audio_cache_path(text: str) -> str:
    """Generate MD5 hash filename from text.
    This is the canonical path generation — all callers must use this function.
    Do NOT duplicate this logic elsewhere."""
    md5_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
    return os.path.join(AUDIO_CACHE_DIR, f"{md5_hash}.mp3")


def get_content_hash(text: str) -> str:
    """Return MD5 hash of text content (without path prefix)."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def register_audio(content_hash: str, story_id: str = None, chapter_file: str = None):
    """Register a new audio entry as 'generating' in DB.
    Called BEFORE TTS generation starts."""
    try:
        from core.database import get_db_session, AudioCacheEntry
        db = get_db_session()
        try:
            existing = db.query(AudioCacheEntry).filter(
                AudioCacheEntry.content_hash == content_hash
            ).first()
            
            if existing:
                # Re-register if previously failed
                if existing.status == "failed":
                    existing.status = "generating"
                    existing.created_at = time.time()
                    db.commit()
                return
            
            entry = AudioCacheEntry(
                content_hash=content_hash,
                file_path=os.path.join(AUDIO_CACHE_DIR, f"{content_hash}.mp3"),
                story_id=story_id,
                chapter_file=chapter_file,
                status="generating",
            )
            db.add(entry)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.error(f"register_audio failed: {e}")


def mark_ready(content_hash: str, file_size: int = 0, duration: float = None):
    """Mark audio as ready after successful TTS generation.
    Called AFTER the MP3 file is fully written to disk."""
    try:
        from core.database import get_db_session, AudioCacheEntry
        db = get_db_session()
        try:
            entry = db.query(AudioCacheEntry).filter(
                AudioCacheEntry.content_hash == content_hash
            ).first()
            
            if entry:
                entry.status = "ready"
                entry.file_size = file_size
                entry.duration = duration
                entry.last_accessed_at = time.time()
                db.commit()
            else:
                # Entry not pre-registered (backward compat) — create it
                new_entry = AudioCacheEntry(
                    content_hash=content_hash,
                    file_path=os.path.join(AUDIO_CACHE_DIR, f"{content_hash}.mp3"),
                    file_size=file_size,
                    duration=duration,
                    status="ready",
                    last_accessed_at=time.time(),
                )
                db.add(new_entry)
                db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.error(f"mark_ready failed: {e}")


def mark_failed(content_hash: str):
    """Mark audio as failed and clean up any corrupt file.
    Called when TTS generation fails (network error, edge-tts crash, etc.)."""
    try:
        # Clean up corrupt file on disk
        file_path = os.path.join(AUDIO_CACHE_DIR, f"{content_hash}.mp3")
        for path in [file_path, file_path + ".part", file_path + ".lock"]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
        
        from core.database import get_db_session, AudioCacheEntry
        db = get_db_session()
        try:
            entry = db.query(AudioCacheEntry).filter(
                AudioCacheEntry.content_hash == content_hash
            ).first()
            
            if entry:
                entry.status = "failed"
                entry.file_size = 0
                db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.error(f"mark_failed failed: {e}")


def touch_accessed(content_hash: str):
    """Update last_accessed_at for LRU eviction tracking.
    Called when audio is served to client."""
    try:
        from core.database import get_db_session, AudioCacheEntry
        db = get_db_session()
        try:
            entry = db.query(AudioCacheEntry).filter(
                AudioCacheEntry.content_hash == content_hash
            ).first()
            if entry:
                entry.last_accessed_at = time.time()
                db.commit()
        finally:
            db.close()
    except Exception as e:
        # Non-critical, just log
        logger.debug(f"touch_accessed failed: {e}")


def cleanup_stale_generating():
    """Startup cleanup: reset entries stuck in 'generating' for >30 minutes.
    Also removes orphaned .lock files."""
    try:
        from core.database import get_db_session, AudioCacheEntry
        db = get_db_session()
        try:
            cutoff = time.time() - 30 * 60  # 30 minutes
            stale = db.query(AudioCacheEntry).filter(
                AudioCacheEntry.status == "generating",
                AudioCacheEntry.created_at < cutoff,
            ).all()
            
            for entry in stale:
                logger.warning(f"Resetting stale generating audio: {entry.content_hash}")
                # Clean up any partial files
                for suffix in ["", ".part", ".lock"]:
                    path = entry.file_path + suffix if suffix else entry.file_path
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                        except Exception:
                            pass
                entry.status = "failed"
            
            if stale:
                db.commit()
                logger.info(f"[CLEANUP] Reset {len(stale)} stale generating audio entries")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"cleanup_stale_generating failed: {e}")
    
    # Also clean orphaned .lock files on disk
    try:
        for f in os.listdir(AUDIO_CACHE_DIR):
            if f.endswith(".lock"):
                lock_path = os.path.join(AUDIO_CACHE_DIR, f)
                # Remove lock files older than 30 minutes
                if time.time() - os.path.getmtime(lock_path) > 30 * 60:
                    os.remove(lock_path)
                    logger.info(f"[CLEANUP] Removed orphaned lock: {f}")
    except Exception as e:
        logger.error(f"Lock cleanup failed: {e}")


def clean_audio_cache():
    """Cleanup cache using DB-backed LRU eviction.
    Policy: >7 days since last access OR total size > 1GB (evict to 800MB)."""
    try:
        from core.database import get_db_session, AudioCacheEntry
        
        max_age = 7 * 24 * 3600  # 7 days
        max_size = 1024 * 1024 * 1024  # 1GB
        target_size = 800 * 1024 * 1024  # 800MB
        now = time.time()
        
        db = get_db_session()
        try:
            # Phase 1: Delete entries older than max_age (by last_accessed_at or created_at)
            old_entries = db.query(AudioCacheEntry).filter(
                AudioCacheEntry.status == "ready",
            ).all()
            
            total_size = 0
            active_entries = []
            
            for entry in old_entries:
                last_used = entry.last_accessed_at or entry.created_at or 0
                age = now - last_used
                
                if age > max_age:
                    # Delete old entry
                    _delete_audio_files(entry.file_path)
                    db.delete(entry)
                    logger.debug(f"Evicted old audio: {entry.content_hash}")
                else:
                    total_size += entry.file_size or 0
                    active_entries.append(entry)
            
            # Phase 2: If still over quota, evict LRU
            if total_size > max_size:
                logger.info(f"Cache {total_size / (1024*1024):.0f}MB exceeds 1GB, evicting LRU...")
                active_entries.sort(key=lambda e: e.last_accessed_at or e.created_at or 0)
                
                for entry in active_entries:
                    if total_size <= target_size:
                        break
                    _delete_audio_files(entry.file_path)
                    total_size -= entry.file_size or 0
                    db.delete(entry)
                    logger.debug(f"Evicted LRU audio: {entry.content_hash}")
            
            # Phase 3: Delete failed entries
            failed = db.query(AudioCacheEntry).filter(
                AudioCacheEntry.status == "failed",
            ).all()
            for entry in failed:
                _delete_audio_files(entry.file_path)
                db.delete(entry)
            
            db.commit()
            logger.debug(f"Audio cache size after cleanup: {total_size / (1024*1024):.0f}MB")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Cache cleanup failed: {e}")


def delete_audio_for_story(story_id: str):
    """Delete ALL audio cache entries and files for a story.
    Called when a story is deleted — ensures complete cleanup."""
    try:
        from core.database import get_db_session, AudioCacheEntry
        db = get_db_session()
        try:
            entries = db.query(AudioCacheEntry).filter(
                AudioCacheEntry.story_id == story_id
            ).all()
            
            deleted = 0
            for entry in entries:
                _delete_audio_files(entry.file_path)
                db.delete(entry)
                deleted += 1
            
            db.commit()
            if deleted:
                logger.info(f"Deleted {deleted} audio cache entries for story: {story_id}")
            return deleted
        finally:
            db.close()
    except Exception as e:
        logger.error(f"delete_audio_for_story failed: {e}")
        return 0


def get_cache_stats() -> dict:
    """Return audio cache statistics for monitoring."""
    try:
        from core.database import get_db_session, AudioCacheEntry
        from sqlalchemy import func
        db = get_db_session()
        try:
            total = db.query(func.count(AudioCacheEntry.content_hash)).scalar() or 0
            ready = db.query(func.count(AudioCacheEntry.content_hash)).filter(
                AudioCacheEntry.status == "ready"
            ).scalar() or 0
            generating = db.query(func.count(AudioCacheEntry.content_hash)).filter(
                AudioCacheEntry.status == "generating"
            ).scalar() or 0
            total_size = db.query(func.sum(AudioCacheEntry.file_size)).filter(
                AudioCacheEntry.status == "ready"
            ).scalar() or 0
            
            return {
                "total": total,
                "ready": ready,
                "generating": generating,
                "failed": total - ready - generating,
                "total_size_mb": round(total_size / (1024 * 1024), 1),
            }
        finally:
            db.close()
    except Exception as e:
        logger.error(f"get_cache_stats failed: {e}")
        return {"error": str(e)}


def _delete_audio_files(file_path: str):
    """Delete audio file and all associated temp files."""
    if not file_path:
        return
    for suffix in ["", ".part", ".lock", ".part.json"]:
        path = file_path + suffix if suffix else file_path
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
