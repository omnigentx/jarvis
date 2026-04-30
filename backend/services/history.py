"""
TTS Cache Manager - SQLite-based implementation.
Manages TTS text cache only. Session/conversation management
has been moved to session_service.py (Fast Agent native sessions).
"""
import logging
from typing import Optional

from core.database import get_db_session, TTSCache

logger = logging.getLogger(__name__)


class TTSCacheManager:
    """
    SQLite-based TTS cache manager.
    Stores text content for TTS generation keyed by request ID.
    """

    def __init__(self):
        pass

    def _get_db(self):
        """Get a database session."""
        return get_db_session()

    def save_tts_text(self, request_id: str, text: str):
        """Save TTS text for a request ID."""
        db = self._get_db()
        try:
            existing = db.query(TTSCache).filter(TTSCache.request_id == request_id).first()
            if existing:
                existing.text = text
            else:
                cache_entry = TTSCache(
                    request_id=request_id,
                    text=text,
                )
                db.add(cache_entry)
            db.commit()
        finally:
            db.close()

    def get_tts_text(self, request_id: str) -> Optional[str]:
        """Get TTS text for a request ID."""
        db = self._get_db()
        try:
            entry = db.query(TTSCache).filter(TTSCache.request_id == request_id).first()
            return entry.text if entry else None
        finally:
            db.close()
