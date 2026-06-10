"""
TTS Pre-generation Job — Background job that pre-generates Edge TTS audio
for story chapters when the server is idle.

Priority queue (dynamic, recalculated on every call):
  P0: Next chapter (n+1) after the one user is currently listening to
  P1: Upcoming chapters (n+2, n+3...) of the same story user is listening to
  P2: First chapter of stories with no audio yet
  P3: Sequential chapters round-robin across remaining stories
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from helpers.path_safety import safe_story_path

import edge_tts

from services.background_jobs import BackgroundJobRunner, BackgroundJobScheduler
from core.database import get_db_session, StoryProgress, BackgroundJobState
from helpers.audio_cache import get_audio_cache_path, get_content_hash, AUDIO_CACHE_DIR, mark_ready, mark_failed, register_audio
from helpers.text_processing import clean_text_for_tts

logger = logging.getLogger("tts_pregen_job")

# Paths — CWD-relative (Docker WORKDIR=/app), consistent with rest of codebase
STORIES_DIR = os.path.join("data", "stories")


class TTSPreGenJob(BackgroundJobRunner):
    """Pre-generate Edge TTS audio for all story chapters."""
    
    job_name = "tts_pregen"
    
    # Config
    DISK_QUOTA_GB = 5.0
    DISK_QUOTA_BYTES = int(DISK_QUOTA_GB * 1024 * 1024 * 1024)
    EVICTION_THRESHOLD = 0.9  # trigger eviction at 90% quota
    CHAPTER_DELAY = 15  # seconds between chapters (rate limit protection)
    VOICE = "vi-VN-NamMinhNeural"
    RATE = "+20%"
    
    def __init__(self, scheduler: BackgroundJobScheduler, pregen_stream=None):
        self.scheduler = scheduler
        self._pregen_stream = pregen_stream  # PregenStreamManager (optional, set later)
        self._stats = {
            "total_chapters": 0,
            "done_chapters": 0,
            "current_story": None,
            "current_chapter": None,
            "disk_usage_mb": 0,
            "errors": 0,
            "last_error": None,
        }
        self.rescan()
    
    def set_pregen_stream(self, stream):
        """Set PregenStreamManager reference (called after both are initialized)."""
        self._pregen_stream = stream
    
    def _emit(self, event: dict):
        """Emit event to PregenStreamManager if available."""
        if self._pregen_stream:
            self._pregen_stream.broadcast(event)
    
    def get_currently_generating(self) -> dict | None:
        """Return the chapter currently being generated, or None.
        
        This is the single source of truth for 'generating' status.
        Lock files on disk are only for write coordination, NOT for status.
        """
        story = self._stats.get("current_story")
        chapter = self._stats.get("current_chapter")
        if story and chapter:
            return {"story_title": story, "chapter_file": chapter}
        return None
    
    def rescan(self):
        """Scan data/stories/ to count total chapters."""
        total = 0
        done = 0
        if os.path.exists(STORIES_DIR):
            for story in sorted(os.listdir(STORIES_DIR)):
                story_path = os.path.join(STORIES_DIR, story)
                if not os.path.isdir(story_path):
                    continue
                for chapter in os.listdir(story_path):
                    if chapter.endswith(".txt"):
                        total += 1
                        # Check if audio already exists
                        text = self._read_chapter_text(story, chapter)
                        if text:
                            cache_path = get_audio_cache_path(clean_text_for_tts(text))
                            if os.path.exists(cache_path):
                                done += 1
        
        self._stats["total_chapters"] = total
        self._stats["done_chapters"] = done
        self._stats["disk_usage_mb"] = self._get_cache_size_mb()
        logger.info(f"[PRE-GEN] Scanned: {done}/{total} chapters already generated, "
                    f"disk: {self._stats['disk_usage_mb']:.0f}MB")
    
    async def get_next_task(self) -> dict | None:
        """Dynamic priority queue: P0 → P1 → P2 → P3."""
        # Check disk quota
        usage_bytes = self._get_cache_size_bytes()
        if usage_bytes > self.DISK_QUOTA_BYTES * self.EVICTION_THRESHOLD:
            evicted = self._evict_old_audio()
            if evicted > 0:
                logger.warning(f"[EVICTION] Evicted {evicted} files, "
                              f"disk now: {self._get_cache_size_mb():.0f}MB")
            # Re-check after eviction
            if self._get_cache_size_bytes() > self.DISK_QUOTA_BYTES * self.EVICTION_THRESHOLD:
                logger.debug("[PRE-GEN] Disk quota still exceeded after eviction, skipping")
                return None
        
        # P0: Next chapter (n+1) for actively listened story
        task = self._find_p0_next_chapter()
        if task:
            task["priority"] = 0
            return task
        
        # P1: Upcoming chapters (n+2, n+3...) of the same actively listened story
        task = self._find_p1_upcoming_chapters()
        if task:
            task["priority"] = 1
            return task
        
        # P2: First chapter of stories with no audio at all
        task = self._find_p2_new_stories()
        if task:
            task["priority"] = 2
            return task
        
        # P3: Sequential chapters across all stories
        task = self._find_p3_sequential()
        if task:
            task["priority"] = 3
            return task
        
        return None  # All chapters generated!
    
    async def execute_task(self, task: dict) -> bool:
        """Generate audio for 1 chapter using Edge TTS with pause/cancel checkpoints."""
        story_title = task["story_title"]
        chapter_file = task["chapter_file"]
        priority = task.get("priority", 9)
        
        logger.debug(f"[PRE-GEN] Generating: {story_title}/{chapter_file} (priority: P{priority})")
        self._stats["current_story"] = story_title
        self._stats["current_chapter"] = chapter_file
        
        # Emit chapter_generating event
        self._emit({
            "type": "chapter_generating",
            "story_id": story_title,
            "chapter_file": chapter_file,
            "priority": priority,
        })
        
        # Read and clean text
        text = self._read_chapter_text(story_title, chapter_file)
        if not text:
            logger.warning(f"[PRE-GEN] Empty text: {story_title}/{chapter_file}")
            return True  # Skip, not an error
        
        text = clean_text_for_tts(text)
        cache_path = get_audio_cache_path(text)
        
        # Already done?
        if os.path.exists(cache_path) and not os.path.exists(cache_path + ".lock"):
            logger.debug(f"[PRE-GEN] Already exists: {story_title}/{chapter_file}")
            self._stats["done_chapters"] += 1
            self._emit({
                "type": "chapter_ready",
                "story_id": story_title,
                "chapter_file": chapter_file,
            })
            return True
        
        lock_path = cache_path + ".lock"
        # Write to a temp file and atomically rename to cache_path ONLY after
        # every chunk succeeds. A present cache_path then always means "complete
        # chapter" — a mid-chapter chunk failure (raise/timeout) can never leave
        # a truncated file that the cache-hit short-circuit above would serve
        # forever (the stuttering/cut-out playback this PR fixes).
        tmp_path = cache_path + ".tmp"
        start_time = time.time()
        bytes_written = 0
        
        try:
            # Create lock file (indicates generation in progress)
            with open(lock_path, "w") as f:
                f.write(json.dumps({
                    "job": "pregen",
                    "story": story_title,
                    "chapter": chapter_file,
                    "started_at": time.time(),
                }))
            
            # Generate TTS in bounded chunks (<= EDGE_MAX_CHUNK) with per-chunk
            # retry + timeout. A single whole-chapter edge_tts request streams
            # slower than playback and intermittently returns no audio /
            # truncates (stuttering playback); small chunks are fast + reliable. Each
            # chunk is buffered so a retried attempt can't duplicate audio.
            from services.tts import EdgeTTSProvider, EDGE_CHUNK_TIMEOUT
            text_chunks = EdgeTTSProvider._split_tiered(text)

            with open(tmp_path, "wb") as audio_file:
                for ci, ctext in enumerate(text_chunks):
                    if not ctext.strip():
                        continue
                    # Pause/cancel checked BETWEEN chunks, NOT inside the
                    # wait_for below: a pause must block here indefinitely until
                    # resume, whereas the per-chunk timeout only guards a stalled
                    # network request. Chunks are small so a cancel (user
                    # switched chapter) lands within a few seconds — this is what
                    # stops a stuck pre-gen from hanging the new chapter.
                    await self.scheduler.check_pause_point()

                    buf = bytearray()
                    for attempt in range(1, 4):
                        buf.clear()

                        async def _consume():
                            communicate = edge_tts.Communicate(ctext, self.VOICE, rate=self.RATE)
                            async for chunk in communicate.stream():
                                if chunk["type"] == "audio" and chunk["data"]:
                                    buf.extend(chunk["data"])

                        try:
                            await asyncio.wait_for(_consume(), timeout=EDGE_CHUNK_TIMEOUT)
                            if buf:
                                break
                        except asyncio.CancelledError:
                            raise  # pause/cancel — let scheduler handle, never retry
                        except Exception as exc:  # incl. TimeoutError (stalled request)
                            if attempt >= 3:
                                raise
                            logger.warning(
                                f"[PRE-GEN] {story_title}/{chapter_file} chunk "
                                f"{ci + 1}/{len(text_chunks)} attempt {attempt} "
                                f"failed ({exc}) — retrying"
                            )
                            await asyncio.sleep(0.4 * attempt)
                    if not buf:
                        raise RuntimeError(
                            f"No audio for chunk {ci + 1}/{len(text_chunks)} after retries"
                        )
                    audio_file.write(buf)
                    bytes_written += len(buf)

            # All chunks succeeded — atomically publish. Until this point only
            # tmp_path exists, so any earlier failure leaves no servable file.
            os.replace(tmp_path, cache_path)

            # Done — remove lock
            if os.path.exists(lock_path):
                os.remove(lock_path)
            
            duration = time.time() - start_time
            size_mb = bytes_written / (1024 * 1024)
            self._stats["done_chapters"] += 1
            self._stats["disk_usage_mb"] = self._get_cache_size_mb()
            
            logger.info(f"[PRE-GEN] Done: {story_title}/{chapter_file} "
                       f"({size_mb:.1f}MB, {duration:.0f}s)")
            
            # Emit chapter_ready event
            self._emit({
                "type": "chapter_ready",
                "story_id": story_title,
                "chapter_file": chapter_file,
                "duration_s": round(duration, 1),
                "size_mb": round(size_mb, 2),
            })
            
            # Rate limit delay
            await asyncio.sleep(self.CHAPTER_DELAY)
            return True
            
        except asyncio.CancelledError:
            # KB2: Clean up partial file
            logger.warning(f"[PRE-GEN] Cancelled: {story_title}/{chapter_file} "
                          f"(after {bytes_written} bytes)")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if os.path.exists(lock_path):
                os.remove(lock_path)
            raise  # Let scheduler handle
            
        except Exception as e:
            logger.error(f"[PRE-GEN] Error: {story_title}/{chapter_file}: {e}",
                        exc_info=True)
            self._stats["errors"] += 1
            self._stats["last_error"] = f"{story_title}/{chapter_file}: {str(e)}"
            # Clean up the partial temp file. cache_path is only created on full
            # success (atomic replace above), so it is never left truncated —
            # drop the old ``bytes_written == 0`` guard that, with chunked
            # writes, left a partial cache_path that got served forever.
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if os.path.exists(lock_path):
                os.remove(lock_path)
            # Emit error event
            self._emit({
                "type": "chapter_error",
                "story_id": story_title,
                "chapter_file": chapter_file,
                "error": str(e),
            })
            return False
    
    def get_status(self) -> dict:
        """Return status for /api/background/status."""
        self._stats["disk_usage_mb"] = self._get_cache_size_mb()
        return {
            "job_name": self.job_name,
            "status": "running" if self._stats["current_chapter"] else "idle",
            "total": self._stats["total_chapters"],
            "done": self._stats["done_chapters"],
            "current_chapter": (
                f"{self._stats['current_story']}/{self._stats['current_chapter']}"
                if self._stats["current_chapter"] else None
            ),
            "disk_usage_mb": self._stats["disk_usage_mb"],
            "disk_quota_mb": int(self.DISK_QUOTA_GB * 1024),
            "errors": self._stats["errors"],
            "last_error": self._stats["last_error"],
        }
    
    # --- Preview queue (for SSE initial snapshot + queue_update) ---
    
    def preview_queue(self, story_id: str | None = None, limit: int = 10) -> list[dict]:
        """Preview the upcoming generation queue without executing.
        
        Returns list of {story_id, chapter_file, priority} in execution order.
        Used by SSE endpoint for initial snapshot and queue_update events.
        """
        queue = []
        seen = set()  # (story_id, chapter_file) to avoid duplicates
        
        # P0: next chapter of active story
        p0_items = self._collect_p0_next_chapter()
        for item in p0_items:
            key = (item["story_title"], item["chapter_file"])
            if key not in seen:
                seen.add(key)
                queue.append({
                    "story_id": item["story_title"],
                    "chapter_file": item["chapter_file"],
                    "priority": 0,
                })
        
        # P1: upcoming chapters (n+2, n+3...) of active story
        p1_items = self._collect_p1_upcoming_chapters(limit=limit)
        for item in p1_items:
            key = (item["story_title"], item["chapter_file"])
            if key not in seen:
                seen.add(key)
                queue.append({
                    "story_id": item["story_title"],
                    "chapter_file": item["chapter_file"],
                    "priority": 1,
                })
        
        # P2: first chapter of new stories
        p2_items = self._collect_p2_new_stories(limit=limit)
        for item in p2_items:
            key = (item["story_title"], item["chapter_file"])
            if key not in seen:
                seen.add(key)
                queue.append({
                    "story_id": item["story_title"],
                    "chapter_file": item["chapter_file"],
                    "priority": 2,
                })
        
        # P3: sequential round-robin
        remaining = limit - len(queue)
        if remaining > 0:
            p3_items = self._collect_p3_sequential(limit=remaining, exclude=seen)
            for item in p3_items:
                queue.append({
                    "story_id": item["story_title"],
                    "chapter_file": item["chapter_file"],
                    "priority": 3,
                })
        
        # Apply story_id filter if requested
        if story_id:
            queue = [q for q in queue if q["story_id"] == story_id]
        
        return queue[:limit]
    
    # --- Priority queue methods ---
    
    def _find_p0_next_chapter(self) -> dict | None:
        """P0: Next chapter (n+1) of the story user is currently listening to."""
        items = self._collect_p0_next_chapter()
        return items[0] if items else None
    
    def _find_p1_upcoming_chapters(self) -> dict | None:
        """P1: Next un-generated chapter (n+2, n+3...) of active story."""
        items = self._collect_p1_upcoming_chapters(limit=1)
        return items[0] if items else None
    
    def _find_p2_new_stories(self) -> dict | None:
        """P2: First chapter of stories that have NO audio generated yet."""
        items = self._collect_p2_new_stories(limit=1)
        return items[0] if items else None
    
    def _find_p3_sequential(self) -> dict | None:
        """P3: Find next un-generated chapter across all stories (round-robin)."""
        items = self._collect_p3_sequential(limit=1)
        return items[0] if items else None
    
    # --- Collector methods (return list for preview_queue) ---
    
    def _collect_p0_next_chapter(self) -> list[dict]:
        """Collect the immediate next chapter (n+1) for active stories."""
        results = []
        try:
            db = get_db_session()
            progresses = db.query(StoryProgress).order_by(
                StoryProgress.last_played_at.desc()
            ).limit(3).all()
            
            for progress in progresses:
                story_dir = os.path.join(STORIES_DIR, progress.story_title)
                if not os.path.exists(story_dir):
                    continue
                
                chapters = sorted([f for f in os.listdir(story_dir) if f.endswith(".txt")])
                
                # Find the chapter AFTER last_chapter_file
                found_current = False
                for chapter_file in chapters:
                    if progress.last_chapter_file and chapter_file == progress.last_chapter_file:
                        found_current = True
                        continue
                    
                    if found_current:
                        # Check if this chapter already has audio
                        text = self._read_chapter_text(progress.story_title, chapter_file)
                        if text:
                            cache_path = get_audio_cache_path(clean_text_for_tts(text))
                            if not os.path.exists(cache_path):
                                results.append({
                                    "story_title": progress.story_title,
                                    "chapter_file": chapter_file,
                                })
                        break  # Only the immediate next (n+1)
            
            db.close()
        except Exception as e:
            logger.error(f"[PRE-GEN] P0 query error: {e}")
        return results
    
    def _collect_p1_upcoming_chapters(self, limit: int = 10) -> list[dict]:
        """Collect chapters n+2, n+3... of the actively listened story.
        
        Skips n+1 (that's P0) and returns subsequent un-generated chapters.
        """
        results = []
        try:
            db = get_db_session()
            progress = db.query(StoryProgress).order_by(
                StoryProgress.last_played_at.desc()
            ).first()
            
            if not progress:
                db.close()
                return results
            
            story_dir = os.path.join(STORIES_DIR, progress.story_title)
            if not os.path.exists(story_dir):
                db.close()
                return results
            
            chapters = sorted([f for f in os.listdir(story_dir) if f.endswith(".txt")])
            
            # Find current position and skip n+1 (P0 handles that)
            found_current = False
            skip_next = True  # Skip the first one after current (that's P0)
            
            for chapter_file in chapters:
                if progress.last_chapter_file and chapter_file == progress.last_chapter_file:
                    found_current = True
                    continue
                
                if found_current:
                    if skip_next:
                        skip_next = False
                        continue  # This is n+1, handled by P0
                    
                    # Check if un-generated
                    text = self._read_chapter_text(progress.story_title, chapter_file)
                    if text:
                        cache_path = get_audio_cache_path(clean_text_for_tts(text))
                        if not os.path.exists(cache_path):
                            results.append({
                                "story_title": progress.story_title,
                                "chapter_file": chapter_file,
                            })
                            if len(results) >= limit:
                                break
            
            db.close()
        except Exception as e:
            logger.error(f"[PRE-GEN] P1 query error: {e}")
        return results
    
    def _collect_p2_new_stories(self, limit: int = 10) -> list[dict]:
        """Collect first chapter of stories that have NO audio generated yet."""
        results = []
        if not os.path.exists(STORIES_DIR):
            return results
        
        for story in sorted(os.listdir(STORIES_DIR)):
            story_path = os.path.join(STORIES_DIR, story)
            if not os.path.isdir(story_path):
                continue
            
            chapters = sorted([f for f in os.listdir(story_path) if f.endswith(".txt")])
            if not chapters:
                continue
            
            # Check if ANY chapter has audio
            has_audio = False
            for chapter in chapters:
                text = self._read_chapter_text(story, chapter)
                if text:
                    cache_path = get_audio_cache_path(clean_text_for_tts(text))
                    if os.path.exists(cache_path):
                        has_audio = True
                        break
            
            if not has_audio:
                first = chapters[0]
                text = self._read_chapter_text(story, first)
                if text:
                    results.append({"story_title": story, "chapter_file": first})
                    if len(results) >= limit:
                        break
        
        return results
    
    def _collect_p3_sequential(self, limit: int = 10, exclude: set | None = None) -> list[dict]:
        """Collect next un-generated chapters across all stories (round-robin)."""
        results = []
        exclude = exclude or set()
        
        if not os.path.exists(STORIES_DIR):
            return results
        
        for story in sorted(os.listdir(STORIES_DIR)):
            story_path = os.path.join(STORIES_DIR, story)
            if not os.path.isdir(story_path):
                continue
            
            chapters = sorted([f for f in os.listdir(story_path) if f.endswith(".txt")])
            for chapter in chapters:
                if (story, chapter) in exclude:
                    continue
                text = self._read_chapter_text(story, chapter)
                if text:
                    clean = clean_text_for_tts(text)
                    cache_path = get_audio_cache_path(clean)
                    if not os.path.exists(cache_path):
                        results.append({"story_title": story, "chapter_file": chapter})
                        if len(results) >= limit:
                            return results
        
        return results
    
    # --- Disk management ---
    
    def _get_cache_size_bytes(self) -> int:
        """Total size of audio_cache directory in bytes."""
        total = 0
        if os.path.exists(AUDIO_CACHE_DIR):
            for f in os.listdir(AUDIO_CACHE_DIR):
                path = os.path.join(AUDIO_CACHE_DIR, f)
                if os.path.isfile(path):
                    total += os.path.getsize(path)
        return total
    
    def _get_cache_size_mb(self) -> float:
        return self._get_cache_size_bytes() / (1024 * 1024)
    
    def _evict_old_audio(self) -> int:
        """Smart eviction: played chapters (oldest first) → unplayed (oldest first)."""
        try:
            # Collect all mp3 files with metadata
            files_with_meta = []
            for f in os.listdir(AUDIO_CACHE_DIR):
                if not f.endswith(".mp3"):
                    continue
                path = os.path.join(AUDIO_CACHE_DIR, f)
                mtime = os.path.getmtime(path)
                size = os.path.getsize(path)
                files_with_meta.append({"path": path, "mtime": mtime, "size": size, "file": f})
            
            if not files_with_meta:
                return 0
            
            # Sort by modification time (oldest first) — simple eviction
            files_with_meta.sort(key=lambda x: x["mtime"])
            
            evicted = 0
            target = self.DISK_QUOTA_BYTES * 0.8  # Evict down to 80%
            current_size = self._get_cache_size_bytes()
            
            for file_info in files_with_meta:
                if current_size <= target:
                    break
                # Don't evict files with .lock (currently generating)
                if os.path.exists(file_info["path"] + ".lock"):
                    continue
                
                os.remove(file_info["path"])
                current_size -= file_info["size"]
                evicted += 1
                logger.debug(f"[EVICTION] Deleted: {file_info['file']} "
                          f"(age: {(time.time() - file_info['mtime'])/3600:.0f}h)")
            
            return evicted
        except Exception as e:
            logger.error(f"[EVICTION] Error: {e}", exc_info=True)
            return 0
    
    # --- Helpers ---
    
    def _read_chapter_text(self, story_title: str, chapter_file: str) -> Optional[str]:
        """Read chapter text file content."""
        # StoryProgress rows can carry attacker-controlled fields (LLM tool
        # write path). Reject `..` / separator components before touching FS.
        try:
            path = safe_story_path(STORIES_DIR, story_title, chapter_file)
        except ValueError as e:
            logger.warning(f"[PRE-GEN] Rejected unsafe path for {story_title!r}/{chapter_file!r}: {e}")
            return None
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
        except Exception as e:
            logger.error(f"[PRE-GEN] Read error: {path}: {e}")
        return None
