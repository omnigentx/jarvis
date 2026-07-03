"""SSoT for per-chapter TTS pre-generation status (the ``story_chapters`` table).

WHY THIS EXISTS
The TTS pre-gen job used to re-derive "which chapters need audio" from disk on
EVERY scheduler tick (~10s): read every chapter's text, ``clean_text_for_tts`` +
md5, then ``os.path.exists`` the cache file. With one real story that was ~1s of
blocking CPU/IO on the event loop every ~10s (561 chapters, 5.2MB re-read + 561
hashes each pass) — measured, and it stalled live turns for ~1s.

Chapter ``.txt`` files are immutable once written, so their content hash is stable.
This module makes SQLite the single source of truth for per-chapter status:

  reconcile(db, sig)   sync the table with disk, re-reading/re-hashing ONLY files
                       whose (mtime, size) changed. Cheap in steady state.
  next_task(db)        pick the next chapter to generate, by priority, via one
                       indexed query — no disk scan, no hashing.
  preview_queue(db)    the upcoming queue for the SSE snapshot.
  status_counts(db)    done/total for the status endpoint — instant.
  mark_ready / mark_pending_by_hash   status transitions from generate / evict.

Readiness is authoritative in this table (``status``): reconcile seeds it from the
mp3's existence for new/changed chapters; generation flips it to ``ready``; eviction
flips it back to ``pending``. The serve path still checks the file directly, so an
out-of-band mp3 deletion is caught there; a ``force`` reconcile re-syncs drift.
"""
from __future__ import annotations

import logging
import os
import time

from sqlalchemy import func

from core.database import StoryChapter, StoryProgress
from helpers.audio_cache import AUDIO_CACHE_DIR, get_content_hash
from helpers.path_safety import safe_story_path
from helpers.text_processing import clean_text_for_tts

logger = logging.getLogger("story_audio_index")

STORIES_DIR = os.path.join("data", "stories")

STATUS_READY = "ready"
STATUS_PENDING = "pending"


# ── disk fingerprint (cheap change-detection) ───────────────────────────────
def signature() -> list[tuple]:
    """A cheap fingerprint of the story tree: sorted ``(story, chapter, mtime,
    size)`` — one ``os.stat`` per chapter, NO read/hash. reconcile() consumes it,
    and the caller compares it to the previous one to skip reconcile when nothing
    changed."""
    sig: list[tuple] = []
    if not os.path.isdir(STORIES_DIR):
        return sig
    for story in sorted(os.listdir(STORIES_DIR)):
        sp = os.path.join(STORIES_DIR, story)
        if not os.path.isdir(sp):
            continue
        for ch in sorted(os.listdir(sp)):
            if not ch.endswith(".txt"):
                continue
            try:
                st = os.stat(os.path.join(sp, ch))
            except OSError:
                continue
            sig.append((story, ch, st.st_mtime, st.st_size))
    return sig


def _read_chapter(story_id: str, chapter_file: str) -> str | None:
    try:
        path = safe_story_path(STORIES_DIR, story_id, chapter_file)
    except ValueError as e:
        logger.warning("[AUDIO-INDEX] unsafe path %r/%r: %s", story_id, chapter_file, e)
        return None
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception as e:  # noqa: BLE001
        logger.error("[AUDIO-INDEX] read error %s: %s", path, e)
    return None


def _hash_and_status(story_id: str, chapter_file: str) -> tuple[str | None, str]:
    """(content_hash, status) for a chapter, read from disk. Only called for NEW or
    CHANGED files during reconcile — the expensive read+hash is amortised away for
    the unchanged majority."""
    text = _read_chapter(story_id, chapter_file)
    if not text:
        return None, STATUS_PENDING
    content_hash = get_content_hash(clean_text_for_tts(text))
    cache_path = os.path.join(AUDIO_CACHE_DIR, f"{content_hash}.mp3")
    return content_hash, (STATUS_READY if os.path.exists(cache_path) else STATUS_PENDING)


# ── reconcile: disk → table (incremental) ───────────────────────────────────
def reconcile(db, sig: list[tuple] | None = None) -> dict:
    """Sync ``story_chapters`` with the story tree. Re-reads/re-hashes ONLY files
    whose ``(mtime, size)`` changed; unchanged chapters keep their content hash and
    status (a position shift just updates ``chapter_num``, no read). Removes rows
    for deleted files. Returns counts. Best-effort per row; commits once."""
    if sig is None:
        sig = signature()
    # Assign chapter_num = sorted position within each story.
    disk: dict[tuple, tuple] = {}
    per_story_idx: dict[str, int] = {}
    for story, ch, mtime, size in sig:
        idx = per_story_idx.get(story, 0)
        disk[(story, ch)] = (idx, mtime, size)
        per_story_idx[story] = idx + 1

    existing = {(r.story_id, r.chapter_file): r for r in db.query(StoryChapter).all()}
    now = time.time()
    changed = removed = 0

    for (story, ch), (num, mtime, size) in disk.items():
        row = existing.get((story, ch))
        if row is None:
            content_hash, status = _hash_and_status(story, ch)
            db.add(StoryChapter(story_id=story, chapter_file=ch, chapter_num=num,
                                content_hash=content_hash, text_mtime=mtime,
                                text_size=size, status=status, updated_at=now))
            changed += 1
        elif row.text_mtime != mtime or row.text_size != size:
            content_hash, status = _hash_and_status(story, ch)
            row.chapter_num, row.content_hash = num, content_hash
            row.text_mtime, row.text_size = mtime, size
            row.status, row.updated_at = status, now
            changed += 1
        elif row.chapter_num != num:
            row.chapter_num = num  # sibling added/removed shifted position — no re-hash

    for key, row in existing.items():
        if key not in disk:
            db.delete(row)
            removed += 1

    db.commit()
    if changed or removed:
        logger.info("[AUDIO-INDEX] reconciled: %d changed, %d removed, %d total",
                    changed, removed, len(disk))
    return {"changed": changed, "removed": removed, "total": len(disk)}


# ── status transitions ──────────────────────────────────────────────────────
def mark_ready(db, story_id: str, chapter_file: str, content_hash: str | None = None) -> None:
    row = db.get(StoryChapter, (story_id, chapter_file))
    if row is None:
        return
    row.status = STATUS_READY
    if content_hash:
        row.content_hash = content_hash
    row.updated_at = time.time()
    db.commit()


def mark_pending_by_hash(db, content_hash: str) -> int:
    """Flip every chapter pointing at an evicted audio file back to pending, so it
    is regenerated. Content-addressed: dedup'd chapters sharing the hash all reset."""
    n = (db.query(StoryChapter)
         .filter(StoryChapter.content_hash == content_hash, StoryChapter.status == STATUS_READY)
         .update({"status": STATUS_PENDING, "updated_at": time.time()},
                 synchronize_session=False))
    db.commit()
    return n


# ── queue / status reads (indexed, no disk) ─────────────────────────────────
def _task(row: StoryChapter, priority: int) -> dict:
    return {"story_title": row.story_id, "chapter_file": row.chapter_file, "priority": priority}


def _chapter_num(db, story_id: str, chapter_file: str | None) -> int | None:
    if not chapter_file:
        return None
    row = db.get(StoryChapter, (story_id, chapter_file))
    return row.chapter_num if row else None


def _active_story(db) -> StoryProgress | None:
    return (db.query(StoryProgress)
            .order_by(StoryProgress.last_played_at.desc()).first())


def _first_story_without_ready(db) -> str | None:
    """First (alphabetical) story that has a pending chapter and NO ready chapter —
    i.e. a story with no audio at all yet (P2)."""
    ready = {s for (s,) in db.query(StoryChapter.story_id)
             .filter(StoryChapter.status == STATUS_READY).distinct()}
    for (sid,) in (db.query(StoryChapter.story_id)
                   .filter(StoryChapter.status == STATUS_PENDING)
                   .order_by(StoryChapter.story_id).distinct()):
        if sid not in ready:
            return sid
    return None


def next_task(db) -> dict | None:
    """The single next chapter to generate, by priority — one indexed query set,
    no disk. P0: next chapter of the actively-listened story. P1: subsequent
    chapters of that story. P2: first chapter of a story with no audio. P3: any
    pending chapter."""
    prog = _active_story(db)
    if prog is not None:
        last_num = _chapter_num(db, prog.story_title, prog.last_chapter_file)
        if last_num is not None:
            row = (db.query(StoryChapter)
                   .filter(StoryChapter.story_id == prog.story_title,
                           StoryChapter.status == STATUS_PENDING,
                           StoryChapter.chapter_num > last_num)
                   .order_by(StoryChapter.chapter_num).first())
            if row is not None:
                return _task(row, 0 if row.chapter_num == last_num + 1 else 1)

    story = _first_story_without_ready(db)
    if story is not None:
        row = (db.query(StoryChapter)
               .filter(StoryChapter.story_id == story, StoryChapter.status == STATUS_PENDING)
               .order_by(StoryChapter.chapter_num).first())
        if row is not None:
            return _task(row, 2)

    row = (db.query(StoryChapter).filter(StoryChapter.status == STATUS_PENDING)
           .order_by(StoryChapter.story_id, StoryChapter.chapter_num).first())
    return _task(row, 3) if row is not None else None


def preview_queue(db, story_id: str | None = None, limit: int = 10) -> list[dict]:
    """The upcoming generation queue in execution order (P0→P3), deduped — for the
    SSE snapshot. Pure DB, no disk."""
    queue: list[dict] = []
    seen: set[tuple] = set()

    def _push(rows, prio):
        for r in rows:
            key = (r.story_id, r.chapter_file)
            if key in seen:
                continue
            seen.add(key)
            queue.append({"story_id": r.story_id, "chapter_file": r.chapter_file, "priority": prio})

    prog = _active_story(db)
    if prog is not None:
        last_num = _chapter_num(db, prog.story_title, prog.last_chapter_file)
        if last_num is not None:
            active = (db.query(StoryChapter)
                      .filter(StoryChapter.story_id == prog.story_title,
                              StoryChapter.status == STATUS_PENDING,
                              StoryChapter.chapter_num > last_num)
                      .order_by(StoryChapter.chapter_num).limit(limit).all())
            _push(active[:1], 0)   # P0: immediate next
            _push(active[1:], 1)   # P1: subsequent

    story = _first_story_without_ready(db)
    if story is not None:
        _push((db.query(StoryChapter)
               .filter(StoryChapter.story_id == story, StoryChapter.status == STATUS_PENDING)
               .order_by(StoryChapter.chapter_num).limit(1).all()), 2)

    if len(queue) < limit:
        _push((db.query(StoryChapter).filter(StoryChapter.status == STATUS_PENDING)
               .order_by(StoryChapter.story_id, StoryChapter.chapter_num)
               .limit(limit).all()), 3)

    if story_id:
        queue = [q for q in queue if q["story_id"] == story_id]
    return queue[:limit]


def status_counts(db) -> dict:
    total = db.query(func.count()).select_from(StoryChapter).scalar() or 0
    ready = (db.query(func.count()).select_from(StoryChapter)
             .filter(StoryChapter.status == STATUS_READY).scalar() or 0)
    return {"total": total, "done": ready, "pending": total - ready}
