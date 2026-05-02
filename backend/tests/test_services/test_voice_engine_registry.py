"""Registry shape and default-config invariants.

The registry is the single source of truth for which engines exist and what
parameters they accept. Settings UI / wizard / factory all read from it, so
breaking the schema breaks every consumer simultaneously. These tests fence
off the contracts that other modules (and the frontend) rely on.
"""
from __future__ import annotations

import pytest

from services import voice_engine_registry as reg


class TestTTSRegistryShape:
    def test_edge_is_present_as_default(self):
        # Edge is the bootstrap default per onboarding rule (free, no API key).
        assert "edge" in reg.TTS_ENGINES
        assert reg.default_tts_chat_config()["engine"] == "edge"

    def test_every_engine_declares_required_keys(self):
        required = {"label", "params", "realtimetts_engine", "output_format"}
        for name, spec in reg.TTS_ENGINES.items():
            missing = required - spec.keys()
            assert not missing, f"engine {name!r} missing keys {missing}"

    def test_secret_engines_declare_secret_keys(self):
        # ElevenLabs/OpenAI/Azure require API keys — declared in spec.secrets so
        # the UI knows to render a masked input + "set secret" button.
        assert "api_key" in reg.TTS_ENGINES["elevenlabs"]["secrets"]
        assert "api_key" in reg.TTS_ENGINES["openai"]["secrets"]
        assert "api_key" in reg.TTS_ENGINES["azure"]["secrets"]
        # Edge is keyless on purpose.
        assert reg.TTS_ENGINES["edge"]["secrets"] == []


class TestSTTRegistryShape:
    def test_faster_whisper_default(self):
        # v1 ships only faster-whisper; the wake-word matrix should expose
        # at least 'off' so the UI can disable the feature.
        assert "faster_whisper" in reg.STT_BACKENDS
        ww = reg.STT_BACKENDS["faster_whisper"]["wake_word_backends"]
        assert "off" in ww
        # Porcupine and OWW should be the two backends we wired up.
        assert {"off", "porcupine", "oww"} <= set(ww.keys())

    def test_default_stt_config_has_auto_language(self):
        cfg = reg.default_stt_config()
        assert cfg["backend"] == "faster_whisper"
        # 'auto' = let Whisper detect — required for vi+en code-switching.
        assert cfg["params"]["language"] == "auto"
        assert cfg["wake_word"]["backend"] == "off"


class TestStoriesSchemaIsLocked:
    def test_default_stories_has_no_engine_field(self):
        # Stories config is locked to Edge — must not surface an 'engine' key
        # so the UI literally cannot offer a paid engine here.
        cfg = reg.default_tts_stories_config()
        assert "engine" not in cfg
        assert "voice" in cfg
        assert "rate" in cfg
