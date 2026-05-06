"""Shared application state - initialized by server.py lifespan, accessed by routes."""
import logging

from services.history import TTSCacheManager
from services.session_service import SessionService
from services.library_manager import LibraryManager
from services.voice_engine_registry import (
    default_tts_chat_config,
    default_tts_stories_config,
)
from services.tts_realtime import build_chat_provider, build_stories_provider

logger = logging.getLogger(__name__)

# Singleton instances (initialized at module load)
tts_cache = TTSCacheManager()
session_service = SessionService()
library_manager = LibraryManager()

# TTS providers — split by audience (see feedback_stories_tts_separation.md):
#   tts_chat_provider     → registry-driven; chat audio + cron notifications.
#                           Default Edge so quick-start works without API keys.
#   tts_stories_provider  → locked Edge; story chapters + library books. Never
#                           swaps to a paid engine even if the user picks one
#                           for chat — protects long-form quota at code level.
# Bootstrap from registry defaults; lifespan rebuilds from DB once config_service
# is available (see runtime_config.apply_voice_chat_config / apply_voice_stories_config).
tts_chat_provider = build_chat_provider(default_tts_chat_config())
tts_stories_provider = build_stories_provider(default_tts_stories_config())
logger.info(
    "Initialized TTS providers: chat=%s stories=%s",
    type(tts_chat_provider).__name__,
    type(tts_stories_provider).__name__,
)

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
stt_recorder = None  # services.stt_realtime.RealtimeSTTService, wired in lifespan

