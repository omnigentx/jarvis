"""apply_voice_chat_config / apply_voice_stories_config / listener dispatch.

Guards that the hot-reload pipeline:
  * rebuilds *only* the chat provider when ``voice.tts.chat`` changes
    (stories must not be touched)
  * rebuilds *only* the stories provider when ``voice.tts.stories`` changes
  * routes the right JSON payload from the ConfigChangeEvent into each
    apply_* function via ``_on_config_change``.

STT apply is exercised separately to keep heavy faster-whisper imports
out of the unit-test path.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from services import runtime_config
from services.config_service import ConfigChangeEvent


class TestApplyVoiceChatConfig:
    def test_rebuilds_chat_provider_only(self):
        from services import shared_state
        original_stories = shared_state.tts_stories_provider

        with patch("services.tts_realtime.build_chat_provider") as build_chat:
            sentinel = MagicMock(name="new_chat")
            build_chat.return_value = sentinel
            with patch("services.runtime_config._get_config_service") as gcs:
                cs = MagicMock()
                cs.get.return_value = None  # no secrets
                gcs.return_value = cs

                runtime_config.apply_voice_chat_config(
                    {"engine": "edge", "params": {"voice": "vi-VN-NamMinhNeural", "rate": "+0%"}}
                )

        assert shared_state.tts_chat_provider is sentinel
        # Stories untouched — this is the core protection invariant.
        assert shared_state.tts_stories_provider is original_stories


class TestApplyVoiceStoriesConfig:
    def test_rebuilds_stories_provider_only(self):
        from services import shared_state
        original_chat = shared_state.tts_chat_provider

        with patch("services.tts_realtime.build_stories_provider") as build_stories:
            sentinel = MagicMock(name="new_stories")
            build_stories.return_value = sentinel
            runtime_config.apply_voice_stories_config(
                {"voice": "vi-VN-HoaiMyNeural", "rate": "+5%"}
            )

        assert shared_state.tts_stories_provider is sentinel
        assert shared_state.tts_chat_provider is original_chat


class TestListenerDispatch:
    """_on_config_change sees a ConfigChangeEvent — must route to the right apply_*."""

    def test_chat_event_calls_chat_apply(self):
        evt = ConfigChangeEvent(
            category="voice",
            key="tts.chat",
            old_value=None,
            new_value=json.dumps({"engine": "edge", "params": {"voice": "x", "rate": "+0%"}}),
            is_secret=False,
            action="update",
        )
        with patch.object(runtime_config, "apply_voice_chat_config") as fn:
            runtime_config._on_config_change(evt)
        fn.assert_called_once()
        # Payload was deserialized before being handed to apply_*
        cfg = fn.call_args.args[0]
        assert cfg["engine"] == "edge"

    def test_stories_event_calls_stories_apply(self):
        evt = ConfigChangeEvent(
            category="voice",
            key="tts.stories",
            old_value=None,
            new_value=json.dumps({"voice": "vi-VN-NamMinhNeural", "rate": "+10%"}),
            is_secret=False,
            action="update",
        )
        with patch.object(runtime_config, "apply_voice_stories_config") as fn:
            runtime_config._on_config_change(evt)
        fn.assert_called_once()

    def test_turn_secret_rotation_invalidates_mint_cache_not_tts(self):
        # TURN creds are WebRTC transport config — rotating them must drop
        # the minted-credential cache (so the next session mints with the
        # new key) and must NOT rebuild the TTS provider.
        from services import webrtc_voice

        evt = ConfigChangeEvent(
            category="voice",
            key="secrets.cloudflare_turn.api_token",
            old_value=None,
            new_value="new-token",
            is_secret=True,
            action="update",
        )
        webrtc_voice._cf_cache["servers"] = [{"urls": "turn:stale"}]
        webrtc_voice._cf_cache["minted_at"] = 12345.0
        with patch.object(runtime_config, "apply_voice_chat_config") as fn:
            runtime_config._on_config_change(evt)
        fn.assert_not_called()
        assert webrtc_voice._cf_cache["servers"] is None
        assert webrtc_voice._cf_cache["minted_at"] == 0.0

    def test_secret_rotation_rebuilds_chat_provider(self):
        # Setting a new ElevenLabs API key must propagate to the live provider
        # without restart, so a hot-reload of the chat factory is the dispatch.
        evt = ConfigChangeEvent(
            category="voice",
            key="secrets.elevenlabs.api_key",
            old_value=None,
            new_value="new-key",
            is_secret=True,
            action="update",
        )
        with patch.object(runtime_config, "apply_voice_chat_config") as fn:
            runtime_config._on_config_change(evt)
        fn.assert_called_once()
