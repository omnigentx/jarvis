"""Voice config bridge — reads structured TTS/STT settings from system_config.

Storage shape (single source of truth — every consumer reads from here):

    voice.tts.chat     → JSON {"engine": "edge", "params": {...}}
    voice.tts.stories  → JSON {"voice": "...", "rate": "+20%"}
    voice.stt          → JSON {"backend": "faster_whisper", "params": {...},
                                "wake_word": {"backend": "off"|"porcupine"|"oww",
                                              "params": {...}}}

Secrets per engine are stored separately so they're encrypted by ConfigService:

    voice.secrets.{engine}.{key}   → e.g. voice.secrets.elevenlabs.api_key

The JSON wrapping (vs flat keys) keeps schema migrations cheap: adding a new
param to an engine doesn't require a DB migration, just a registry update +
an opaque blob in the existing key.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from services import voice_engine_registry as registry

logger = logging.getLogger(__name__)


VOICE_TTS_CHAT_KEY = ("voice", "tts.chat")
VOICE_TTS_STORIES_KEY = ("voice", "tts.stories")
VOICE_STT_KEY = ("voice", "stt")


def _load_json(config_service, category: str, key: str) -> Optional[dict[str, Any]]:
    raw = config_service.get(category, key)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        logger.warning("[voice_config] invalid JSON in %s/%s, ignoring", category, key)
        return None


def get_chat_config(config_service) -> dict[str, Any]:
    return _load_json(config_service, *VOICE_TTS_CHAT_KEY) or registry.default_tts_chat_config()


def get_stories_config(config_service) -> dict[str, Any]:
    return _load_json(config_service, *VOICE_TTS_STORIES_KEY) or registry.default_tts_stories_config()


def get_stt_config(config_service) -> dict[str, Any]:
    return _load_json(config_service, *VOICE_STT_KEY) or registry.default_stt_config()


def set_chat_config(config_service, config: dict[str, Any], updated_by: str = "voice_api") -> None:
    _validate_chat_config(config)
    config_service.set("voice", "tts.chat", json.dumps(config), source=updated_by)


def set_stories_config(config_service, config: dict[str, Any], updated_by: str = "voice_api") -> None:
    _validate_stories_config(config)
    config_service.set("voice", "tts.stories", json.dumps(config), source=updated_by)


def set_stt_config(config_service, config: dict[str, Any], updated_by: str = "voice_api") -> None:
    _validate_stt_config(config)
    config_service.set("voice", "stt", json.dumps(config), source=updated_by)


def get_engine_secrets(config_service, engine: str) -> dict[str, str]:
    """Return {secret_key: plaintext} for a given engine. Empty if none set."""
    spec = registry.get_tts_engine(engine)
    if not spec:
        return {}
    out: dict[str, str] = {}
    for sk in spec.get("secrets", []):
        val = config_service.get("voice", f"secrets.{engine}.{sk}")
        if val:
            out[sk] = val
    return out


def set_engine_secret(config_service, engine: str, secret_key: str, value: str, updated_by: str = "voice_api") -> None:
    spec = registry.get_tts_engine(engine)
    if not spec or secret_key not in spec.get("secrets", []):
        raise ValueError(f"Engine {engine!r} has no declared secret {secret_key!r}")
    config_service.set(
        "voice",
        f"secrets.{engine}.{secret_key}",
        value,
        is_secret=True,
        source=updated_by,
    )


# ---- validation -------------------------------------------------------------


def _validate_chat_config(config: dict[str, Any]) -> None:
    engine = config.get("engine")
    if engine not in registry.TTS_ENGINES:
        raise ValueError(f"Unknown TTS engine: {engine!r}")
    if "params" in config and not isinstance(config["params"], dict):
        raise ValueError("'params' must be a dict")


def _validate_stories_config(config: dict[str, Any]) -> None:
    # Stories schema is locked: only voice + rate, no engine field.
    if "engine" in config:
        raise ValueError("Stories TTS is locked to Edge — 'engine' field not allowed")
    for k in config.keys():
        if k not in {"voice", "rate"}:
            raise ValueError(f"Unexpected stories config key: {k!r}")


def _validate_stt_config(config: dict[str, Any]) -> None:
    backend = config.get("backend")
    if backend not in registry.STT_BACKENDS:
        raise ValueError(f"Unknown STT backend: {backend!r}")
    ww = config.get("wake_word") or {}
    ww_backend = ww.get("backend", "off")
    spec = registry.STT_BACKENDS[backend]
    if ww_backend not in spec.get("wake_word_backends", {}):
        raise ValueError(f"Unknown wake-word backend: {ww_backend!r}")
