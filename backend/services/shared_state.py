"""Shared application state - initialized by server.py lifespan, accessed by routes."""
import logging

from services.history import TTSCacheManager
from services.session_service import SessionService
from services.library_manager import LibraryManager
from services.tts import TTSFactory

logger = logging.getLogger(__name__)

# Singleton instances (initialized at module load)
tts_cache = TTSCacheManager()
session_service = SessionService()
library_manager = LibraryManager()
tts_provider = TTSFactory.get_provider()
logger.info(f"Initialized TTS Provider: {type(tts_provider).__name__}")

# Mutable state (set during lifespan)
agent_app = None
bg_scheduler = None
generation_tasks = {}
crawl_poller = None
spawn_bridge = None  # SpawnProgressBridge instance, wired in lifespan
registry_db = None  # AgentRegistryDB instance, wired in lifespan
meeting_event_manager = None  # MeetingEventManager, wired in lifespan
meeting_bridge = None  # MeetingEventBridge, wired in lifespan
cron_scheduler = None  # CronScheduler instance, wired in lifespan
current_conversation_id = None  # Active chat conversation_id (set during chat processing)

