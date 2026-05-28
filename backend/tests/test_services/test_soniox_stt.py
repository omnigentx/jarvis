"""Soniox STT token-protocol unit tests.

The WebSocket roundtrip is not exercised here — that's covered by the
shared :class:`WSStreamingSTT` scaffolding and a future fake-server e2e.
What matters per-provider is the Soniox-specific bits: which keys land
in the config message, how the ``<end>`` token flushes the buffer, and
how provisional vs finalized tokens accumulate.
"""
from __future__ import annotations

import pytest

from services.stt_backends.soniox import SonioxSTTService


def _make_service(**params):
    return SonioxSTTService(api_key="sk-fake", params=params)


def _hook_recorder(svc):
    events: list[tuple[str, dict]] = []
    svc.set_hook(lambda ev, payload: events.append((ev, payload)))
    return events


class TestConfigMessage:
    def test_defaults(self):
        svc = _make_service()
        cfg = svc._build_config_message()
        assert cfg["api_key"] == "sk-fake"
        assert cfg["audio_format"] == "pcm_s16le"
        assert cfg["sample_rate"] == 16000
        assert cfg["num_channels"] == 1
        assert cfg["enable_endpoint_detection"] is True
        # Optional flags are omitted unless explicitly enabled, keeping the
        # message minimal so Soniox doesn't reject unknown nullable fields
        # on future API versions.
        assert "language_hints" not in cfg
        assert "enable_language_identification" not in cfg
        assert "enable_speaker_diarization" not in cfg

    def test_language_hints_csv_to_list(self):
        svc = _make_service(language_hints="vi, en")
        assert svc._build_config_message()["language_hints"] == ["vi", "en"]

    def test_language_hints_blank_is_omitted(self):
        svc = _make_service(language_hints="   ")
        cfg = svc._build_config_message()
        assert "language_hints" not in cfg

    def test_optional_flags_passthrough(self):
        svc = _make_service(
            enable_language_identification=True,
            enable_speaker_diarization=True,
            model="stt-rt-v4",
            sample_rate=24000,
        )
        cfg = svc._build_config_message()
        assert cfg["model"] == "stt-rt-v4"
        assert cfg["sample_rate"] == 24000
        assert cfg["enable_language_identification"] is True
        assert cfg["enable_speaker_diarization"] is True


class TestTokenHandling:
    def test_provisional_tokens_emit_running_partial(self):
        svc = _make_service()
        events = _hook_recorder(svc)
        svc._handle_event({"tokens": [{"text": "hello", "is_final": False}]})
        svc._handle_event({"tokens": [{"text": "hello there", "is_final": False}]})
        partials = [p["text"] for ev, p in events if ev == "partial_transcript"]
        # The provisional tail is replaced (not appended), so the second
        # frame supersedes the first instead of building "hellohello there".
        assert partials == ["hello", "hello there"]

    def test_multiple_provisional_tokens_in_one_frame_concatenate(self):
        # Soniox can pack several non-final tokens into a single message.
        # Old behaviour (``_provisional_tail = text`` per token) collapsed
        # the frame down to the last token, dropping "hello " from
        # ``[{"text":"hello ","is_final":False}, {"text":"world","is_final":False}]``.
        # The accumulator now resets at the start of ``_handle_event`` and
        # concatenates within the loop, so every provisional in the frame
        # contributes to the running partial.
        svc = _make_service()
        events = _hook_recorder(svc)
        svc._handle_event({"tokens": [
            {"text": "hello ", "is_final": False},
            {"text": "world", "is_final": False},
        ]})
        partials = [p["text"] for ev, p in events if ev == "partial_transcript"]
        assert partials == ["hello world"]

    def test_final_tokens_accumulate_into_buffer(self):
        svc = _make_service()
        events = _hook_recorder(svc)
        svc._handle_event({"tokens": [
            {"text": "Xin ", "is_final": True},
            {"text": "chào", "is_final": True},
        ]})
        partials = [p["text"] for ev, p in events if ev == "partial_transcript"]
        assert partials[-1] == "Xin chào"
        # No final yet — endpoint hasn't arrived.
        assert not any(ev == "final_transcript" for ev, _ in events)

    def test_endpoint_token_flushes_final_and_resets(self):
        svc = _make_service()
        events = _hook_recorder(svc)
        svc._handle_event({"tokens": [
            {"text": "Hello ", "is_final": True},
            {"text": "world", "is_final": True},
            {"text": "<end>", "is_final": True},
        ]})
        finals = [p["text"] for ev, p in events if ev == "final_transcript"]
        assert finals == ["Hello world"]
        # Endpoint emits vad_stop → recording_stop → recording_start so the
        # voice WS route resets its per-turn state and is ready for the
        # next utterance on the same Soniox connection.
        sequence = [ev for ev, _ in events if ev in {"vad_stop", "recording_stop", "recording_start"}]
        assert sequence == ["vad_stop", "recording_stop", "recording_start"]

        # Buffer must be empty so the next utterance doesn't carry over.
        events.clear()
        svc._handle_event({"tokens": [{"text": "Next", "is_final": True}]})
        partials = [p["text"] for ev, p in events if ev == "partial_transcript"]
        assert partials == ["Next"]

    def test_endpoint_without_any_text_does_not_emit_empty_final(self):
        svc = _make_service()
        events = _hook_recorder(svc)
        svc._handle_event({"tokens": [{"text": "<end>", "is_final": True}]})
        assert not any(ev == "final_transcript" for ev, _ in events)
        # Endpoint signal still fires so downstream resets per-turn state
        # even when the model produced no audible speech.
        assert ("vad_stop", {}) in events

    def test_error_message_surfaces_as_error_event(self):
        svc = _make_service()
        events = _hook_recorder(svc)
        svc._handle_event({"error_code": 401, "error_message": "bad key"})
        assert ("error", {"detail": "bad key"}) in events


class TestBuildFactory:
    def test_missing_api_key_raises(self, monkeypatch):
        from services.config_service import config_service
        monkeypatch.setattr(config_service, "get", lambda *a, **kw: None)
        from services.stt_backends.soniox import build
        with pytest.raises(RuntimeError, match="no API key"):
            build({"params": {}})
