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
        # The default-backend is faster-whisper (multilingual). Its wake-word
        # matrix must expose at least 'off' so the UI can disable the feature.
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


class TestGipformerVIBackend:
    """Plug-and-play smoke test for the Vietnamese-only Gipformer backend."""

    def test_registered(self):
        assert "gipformer_vi" in reg.STT_BACKENDS

    def test_language_locked_to_vi(self):
        # The backend is Vietnamese-only; UI relies on language_locked to
        # hide the language picker. Anything other than 'vi' here would
        # let the UI offer a setting the engine can't honour.
        spec = reg.STT_BACKENDS["gipformer_vi"]
        assert spec.get("language_locked") == "vi"

    def test_wake_word_only_off(self):
        # No Porcupine/OWW plumbing for sherpa-onnx — the spec should
        # surface only the 'off' option so the UI never offers a config
        # the runtime would silently ignore.
        ww = reg.STT_BACKENDS["gipformer_vi"]["wake_word_backends"]
        assert set(ww.keys()) == {"off"}

    def test_required_params_present(self):
        # Each of these keys is read in services/stt_backends/gipformer_vi.py
        # and the UI form is rendered straight off this list.
        keys = {p["key"] for p in reg.STT_BACKENDS["gipformer_vi"]["params"]}
        assert {"quantize", "decoding_method", "num_threads",
                "silero_sensitivity", "post_speech_silence_duration",
                "min_speech_duration"} <= keys


class TestSTTDispatcher:
    """``build_stt_service`` dispatches on ``config['backend']``."""

    def test_known_factory_table_matches_registry(self):
        # Every registry backend must have a factory wired up; otherwise
        # picking that backend in the UI would crash on first build.
        from services.stt_realtime import _BACKEND_FACTORIES
        assert set(_BACKEND_FACTORIES.keys()) == set(reg.STT_BACKENDS.keys())

    def test_unknown_backend_rejected(self):
        from services.stt_realtime import build_stt_service
        with pytest.raises(ValueError, match="Unknown STT backend"):
            build_stt_service({"backend": "nope"})

    def test_dispatcher_calls_factory_for_each_backend(self, monkeypatch):
        # Patch each backend module's ``build`` so we can confirm dispatch
        # routes to the right one without actually loading models.
        from services import stt_realtime
        from services.stt_backends import faster_whisper as fw_mod
        from services.stt_backends import gipformer_vi as gv_mod

        called: dict[str, dict] = {}
        monkeypatch.setattr(fw_mod, "build", lambda c: called.setdefault("fw", c))
        monkeypatch.setattr(gv_mod, "build", lambda c: called.setdefault("gv", c))

        stt_realtime.build_stt_service({"backend": "faster_whisper", "params": {"a": 1}})
        stt_realtime.build_stt_service({"backend": "gipformer_vi", "params": {"b": 2}})

        assert called["fw"]["params"]["a"] == 1
        assert called["gv"]["params"]["b"] == 2


class TestStoriesSchemaIsLocked:
    def test_default_stories_has_no_engine_field(self):
        # Stories config is locked to Edge — must not surface an 'engine' key
        # so the UI literally cannot offer a paid engine here.
        cfg = reg.default_tts_stories_config()
        assert "engine" not in cfg
        assert "voice" in cfg
        assert "rate" in cfg
