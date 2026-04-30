"""
Library Manager - SQLite-based implementation.
Manages audio book library entries.
Single-user mode — no user isolation.
"""
import os
import time
import uuid
import logging
from typing import List, Optional
from pydantic import BaseModel

from core.database import get_db_session, Book

logger = logging.getLogger(__name__)


class AudioBook(BaseModel):
    """Pydantic model for AudioBook (for API responses)."""
    id: str
    title: str
    chapter: str
    url: str
    file_path: str
    duration: float = 0.0
    current_time: float = 0.0
    status: str = "generating"
    created_at: float

    class Config:
        from_attributes = True


class LibraryManager:
    """SQLite-based library manager."""
    
    def __init__(self, data_dir: str = "data"):
        self.data_dir = data_dir
        self.audio_cache_dir = os.path.join(data_dir, "audio_cache")
        os.makedirs(self.audio_cache_dir, exist_ok=True)
    
    def _get_db(self):
        """Get a database session."""
        return get_db_session()
    
    @staticmethod
    def _safe_float(val, default=0.0) -> float:
        """Convert value to float, handling HH:MM:SS strings from old DB schema."""
        if val is None:
            return default
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            # Try HH:MM:SS or MM:SS
            parts = val.split(":")
            try:
                if len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                if len(parts) == 2:
                    return int(parts[0]) * 60 + float(parts[1])
                return float(val)
            except (ValueError, IndexError):
                return default
        return default

    def _book_to_audiobook(self, book: Book) -> AudioBook:
        """Convert database Book to AudioBook Pydantic model."""
        return AudioBook(
            id=book.id,
            title=book.title,
            chapter=book.chapter or "",
            url=book.url or "",
            file_path=book.file_path or "",
            duration=self._safe_float(book.duration),
            current_time=self._safe_float(book.current_time),
            status=book.status or "generating",
            created_at=book.created_at or 0
        )

    def add_book(self, title: str, chapter: str, url: str, 
                 id_override: str = None) -> AudioBook:
        """Register a new book or return existing if URL/ID matches."""
        db = self._get_db()
        try:
            # Check specific ID override first
            if id_override:
                existing = db.query(Book).filter(
                    Book.id == id_override
                ).first()

                if existing:
                    dirty = False
                    if title != "Unknown Story" and (existing.title != title or existing.chapter != chapter):
                        existing.title = title
                        existing.chapter = chapter
                        dirty = True
                    if dirty:
                        db.commit()
                        logger.info(f"Updated book {existing.id}")
                    return self._book_to_audiobook(existing)
            
            # Check by URL to avoid duplicates
            if not id_override:
                existing = db.query(Book).filter(
                    Book.url == url,
                ).first()
                
                if existing:
                    if title != "Unknown Story" and (existing.title != title or existing.chapter != chapter):
                        existing.title = title
                        existing.chapter = chapter
                        db.commit()
                        logger.info(f"Updated metadata for book {existing.id}")
                    return self._book_to_audiobook(existing)
            
            # Create new book
            book_id = id_override if id_override else str(uuid.uuid4())
            file_path = ""  # Updated to audio_cache/{hash}.mp3 when text hash is computed
            
            book = Book(
                id=book_id,
                title=title,
                chapter=chapter,
                url=url,
                file_path=file_path,
                created_at=time.time()
            )
            
            db.add(book)
            db.commit()
            db.refresh(book)
            
            return self._book_to_audiobook(book)
        finally:
            db.close()

    def get_book(self, book_id: str) -> Optional[AudioBook]:
        """Get a book by ID."""
        db = self._get_db()
        try:
            book = db.query(Book).filter(Book.id == book_id).first()
            return self._book_to_audiobook(book) if book else None
        finally:
            db.close()

    def get_all_books(self) -> List[AudioBook]:
        """Get all books with status/duration sync."""
        try:
            from mutagen.mp3 import MP3
        except ImportError:
            MP3 = None

        db = self._get_db()
        try:
            books_db = db.query(Book).order_by(Book.created_at.desc()).all()
            
            results = []
            is_dirty = False
            
            for book in books_db:
                lock_path = (book.file_path or "") + ".lock"
                
                if book.file_path and os.path.exists(book.file_path):
                    # Only mark as ready if NO lock file exists
                    if not os.path.exists(lock_path):
                        if book.status != "ready":
                            book.status = "ready"
                            is_dirty = True
                        
                        # Validate and update duration
                        status_ok = True
                        if book.duration and book.duration > 0:
                            size = os.path.getsize(book.file_path)
                            if size > 1024 and (size / book.duration) > 64000:
                                logger.warning(f"Detected invalid duration for {book.title}")
                                book.duration = 0
                                status_ok = False
                        
                        if (book.duration or 0) <= 0 or not status_ok:
                            try:
                                if MP3:
                                    audio = MP3(book.file_path)
                                    book.duration = audio.info.length
                                    is_dirty = True
                                    logger.info(f"Updated duration for {book.title}: {book.duration}s")
                                else:
                                    # Fallback estimation
                                    size = os.path.getsize(book.file_path)
                                    if size > 0:
                                        book.duration = size / 4000.0
                                        is_dirty = True
                            except Exception as e:
                                logger.error(f"Error reading duration for {book.file_path}: {e}")
                
                results.append(self._book_to_audiobook(book))
            
            if is_dirty:
                db.commit()
            
            return results
        finally:
            db.close()

    def update_progress(self, book_id: str, new_time: float):
        """Update playback progress for a book."""
        db = self._get_db()
        try:
            book = db.query(Book).filter(Book.id == book_id).first()
            if book:
                book.current_time = new_time
                book.last_played_at = time.time()
                if book.status == "generating" and book.file_path and os.path.exists(book.file_path):
                    book.status = "ready"
                db.commit()
        finally:
            db.close()

    def set_status(self, book_id: str, status: str, duration: float = None):
        """Set status for a book, optionally update duration."""
        db = self._get_db()
        try:
            book = db.query(Book).filter(Book.id == book_id).first()
            if book:
                book.status = status
                if duration is not None:
                    book.duration = duration
                db.commit()
        finally:
            db.close()

    def update_file_path(self, book_id: str, new_file_path: str):
        """Update file_path to point to the correct audio cache location."""
        db = self._get_db()
        try:
            book = db.query(Book).filter(Book.id == book_id).first()
            if book and book.file_path != new_file_path:
                book.file_path = new_file_path
                db.commit()
        finally:
            db.close()

    def delete_book(self, book_id: str):
        """Delete a book and its files."""
        db = self._get_db()
        try:
            book = db.query(Book).filter(Book.id == book_id).first()
            if book:
                file_path = book.file_path
                
                # Delete files
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except:
                        pass
                
                part_path = file_path + ".part" if file_path else None
                if part_path and os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except:
                        pass
                
                db.delete(book)
                db.commit()
        finally:
            db.close()
