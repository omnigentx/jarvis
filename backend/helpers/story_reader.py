"""Story reader helpers: handle_read_local, check_pending_read."""
import os
import re
import json
import time
import logging
from pathlib import Path

from helpers.path_safety import safe_story_path
from helpers.text_processing import clean_text_for_tts

logger = logging.getLogger(__name__)


def build_story_meta(story_title, chapter_file):
    """Build the playlist metadata the chat ``done`` event hands the frontend
    singleton player, so a story started from chat behaves exactly like one
    started from the Stories library (chapter nav, progress, resume).

    ``story_id`` is the folder name — identical to the id the ``/api/stories``
    routes use — so the frontend replays through the same ``playChapter`` path
    and reuses the TTS cache this request already populated.

    Returns ``None`` when inputs are missing so the caller falls back to plain
    chat-TTS playback.
    """
    if not story_title or not chapter_file:
        return None
    story_dir = os.path.join("data/stories", story_title)
    chapter_files = []
    if os.path.isdir(story_dir):
        chapter_files = sorted(f for f in os.listdir(story_dir) if f.endswith(".txt"))
    return {
        "story_id": story_title,
        "story_title": story_title,
        "chapter_file": chapter_file,
        "chapter_files": chapter_files,
    }


def handle_read_local(story_title: str, chapter_filename: str, library_manager, tts_cache) -> dict:
    """
    Handle [[[READ_LOCAL: story_title|chapter_filename]]] tag.
    Reads local chapter text, creates a library entry, saves TTS text.
    Returns dict with keys: book_id, tts_text on success, or error on failure.
    """
    # PendingAction payload originates from LLM tool calls — guard against
    # `../../etc/passwd`-style chapter_filename or story_title before any
    # filesystem touch.
    try:
        path = safe_story_path(Path("data") / "stories", story_title, chapter_filename)
    except ValueError as e:
        logger.warning(f"handle_read_local rejected unsafe payload {story_title!r}/{chapter_filename!r}: {e}")
        return {"error": "Invalid story/chapter path"}

    if not path.exists():
        return {"error": f"File not found: {story_title}/{chapter_filename}"}

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()

        text = clean_text_for_tts(text)

        if not text.strip():
            return {"error": "Chapter content is empty"}

        safe_fname = re.sub(r'[^\w\-\.]', '_', chapter_filename)
        unique_id = f"story_{story_title}_{safe_fname}"

        chapter_name = chapter_filename.replace(".txt", "")
        if "_" in chapter_filename:
            parts = chapter_filename.split("_", 1)
            if len(parts) > 1:
                chapter_name = parts[1].replace(".txt", "").replace("_", " ")

        library_manager.add_book(
            story_title,
            chapter_name,
            url=f"local://{story_title}/{chapter_filename}",
            id_override=unique_id,
        )

        tts_cache.save_tts_text(unique_id, text)

        # story_title/chapter_file let the chat route build playlist metadata
        # (build_story_meta) so chat playback routes through the singleton
        # story player instead of a one-off TTS clip.
        return {
            "book_id": unique_id,
            "tts_text": text,
            "story_title": story_title,
            "chapter_file": chapter_filename,
        }

    except Exception as e:
        logger.error(f"handle_read_local error: {e}", exc_info=True)
        return {"error": str(e)}


def check_pending_read(library_manager, tts_cache, get_chapter_content_fn) -> dict | None:
    """
    Check for a pending read action from DB (PendingAction table).
    Returns dict with 'book_id' and 'tts_text' if found, None otherwise.
    Replaces old file-based pending_read.json approach.
    """
    try:
        from core.database import get_db_session, PendingAction
        db = get_db_session()
        try:
            # Pop the oldest pending action (FIFO)
            action_row = db.query(PendingAction).order_by(PendingAction.id.asc()).first()
            if not action_row:
                return None
            
            # Delete immediately (consume)
            action = json.loads(action_row.payload_json)
            created_at = action_row.created_at or 0
            db.delete(action_row)
            db.commit()
            
            # TTL check: ignore if older than 60s
            if time.time() - created_at > 60:
                logger.warning("Pending read action expired, ignoring.")
                return None
            
            action_type = action.get("type")
            logger.debug(f"Processing pending read action: {action_type}")

            if action_type == "READ_LOCAL":
                result = handle_read_local(action["story_title"], action["chapter_filename"],
                                           library_manager, tts_cache)
                if "error" not in result:
                    return {
                        "book_id": result["book_id"],
                        "tts_text": result["tts_text"],
                        "story_title": result.get("story_title"),
                        "chapter_file": result.get("chapter_file"),
                    }
                else:
                    logger.warning(f"Pending READ_LOCAL failed: {result['error']}")

            elif action_type == "READ_LIBRARY":
                book = library_manager.get_book(action["book_id"])
                if book:
                    return {"book_id": book.id, "tts_text": "Content from library"}
                else:
                    logger.warning(f"Pending READ_LIBRARY: book not found: {action['book_id']}")

            elif action_type == "READ_STORY":
                story_url = action["url"]
                story_content = get_chapter_content_fn(story_url)
                if story_content and not story_content.startswith("Failed"):
                    title = "Story"
                    chapter = "Unknown"
                    meta_match = re.search(r"\[\[METADATA:\s*(.*?)\]\]", story_content, re.DOTALL)
                    if meta_match:
                        full_title = meta_match.group(1).strip()
                        story_content = story_content.replace(meta_match.group(0), "").strip()
                        if " - " in full_title:
                            parts = full_title.rsplit(" - ", 1)
                            title = parts[0].strip()
                            chapter = parts[1].strip()
                        else:
                            chapter = full_title
                    book = library_manager.add_book(title, chapter, story_url)
                    return {"book_id": book.id, "tts_text": story_content}
                else:
                    logger.warning(f"Pending READ_STORY: fetch failed for {story_url}")
        finally:
            db.close()

    except Exception as e:
        logger.error(f"check_pending_read error: {e}", exc_info=True)

    return None

