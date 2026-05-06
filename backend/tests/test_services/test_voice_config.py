"""voice_config bridge — JSON round-trip against an in-memory ConfigService.

Covers: JSON serialization, validator rejections that protect the locked
stories schema and the engine-must-exist invariant, and engine-secret
isolation per spec.
"""
from __future__ import annotations

import pytest

from services import voice_config as vc
from services import voice_engine_registry as reg
from services.config_service import ConfigService


@pytest.fixture()
def cs(tmp_path, monkeypatch):
    """Per-test in-memory ConfigService over a tmpfile DB.

    Swapping the SessionLocal globally (like test_setup_gate does) is overkill
    here — voice_config helpers take the service explicitly so we just inject
    a fresh one bound to its own engine. A master key is set so secret writes
    can encrypt — without it ConfigService.set raises MissingMasterKeyError.
    """
    monkeypatch.setenv("JARVIS_API_KEY", "voice-config-test-master-key-1234")
    from core import secrets_crypto
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.database import Base
    eng = create_engine(f"sqlite:///{tmp_path}/voice_cfg.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    yield ConfigService(db_factory=Session)
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None


class TestRoundTrip:
    def test_chat_config_default_when_unset(self, cs):
        # No row in DB → registry default surfaces, so first-run users get
        # working Edge config without manual setup.
        assert vc.get_chat_config(cs)["engine"] == "edge"

    def test_chat_config_persists_and_reads_back(self, cs):
        new_cfg = {"engine": "elevenlabs", "params": {"voice": "Rachel", "model": "eleven_multilingual_v2"}}
        vc.set_chat_config(cs, new_cfg)
        assert vc.get_chat_config(cs) == new_cfg

    def test_stories_config_round_trip(self, cs):
        vc.set_stories_config(cs, {"voice": "vi-VN-HoaiMyNeural", "rate": "+10%"})
        assert vc.get_stories_config(cs) == {"voice": "vi-VN-HoaiMyNeural", "rate": "+10%"}

    def test_stt_config_round_trip(self, cs):
        cfg = {
            "backend": "faster_whisper",
            "params": {"model": "small", "language": "vi"},
            "wake_word": {"backend": "off", "params": {}},
        }
        vc.set_stt_config(cs, cfg)
        assert vc.get_stt_config(cs) == cfg


class TestValidators:
    def test_unknown_chat_engine_rejected(self, cs):
        # An invalid engine in the JSON would crash the factory at apply time;
        # validator catches it at the API boundary instead.
        with pytest.raises(ValueError):
            vc.set_chat_config(cs, {"engine": "nope", "params": {}})

    def test_stories_config_rejects_engine_field(self, cs):
        # The whole point of split TTS — stories must never carry an 'engine'
        # field that could reroute long-form to a paid backend.
        with pytest.raises(ValueError):
            vc.set_stories_config(cs, {"engine": "elevenlabs", "voice": "x"})

    def test_stories_config_rejects_unknown_keys(self, cs):
        with pytest.raises(ValueError):
            vc.set_stories_config(cs, {"voice": "x", "extra": "y"})

    def test_stt_unknown_wake_word_rejected(self, cs):
        with pytest.raises(ValueError):
            vc.set_stt_config(cs, {
                "backend": "faster_whisper",
                "params": {},
                "wake_word": {"backend": "magic", "params": {}},
            })


class TestSecrets:
    def test_engine_secret_round_trip(self, cs):
        vc.set_engine_secret(cs, "elevenlabs", "api_key", "sk-secret-123")
        secrets = vc.get_engine_secrets(cs, "elevenlabs")
        assert secrets["api_key"] == "sk-secret-123"

    def test_engine_secret_undeclared_slot_rejected(self, cs):
        # Edge declares no secrets — setting one shouldn't be allowed because
        # the registry is the contract, not the DB.
        with pytest.raises(ValueError):
            vc.set_engine_secret(cs, "edge", "api_key", "anything")
