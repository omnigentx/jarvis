"""REST surface for /api/voice/*.

These are end-to-end through FastAPI's TestClient because the route layer
depends on auth, the ConfigService singleton, and the StreamingResponse
generator — patching all of that piecewise is more fragile than running
the actual stack with a temp DB.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolated DB per test so config writes don't leak across cases.
    db_file = tmp_path / "voice_routes.db"
    monkeypatch.setenv("JARVIS_DB_PATH", str(db_file))
    monkeypatch.setenv("JARVIS_API_KEY", "voice-routes-test-master-key")

    # core_auth caches the env var at import — sync the module attr so
    # verify_api_key sees our test key (matches existing test_setup_gate pattern).
    from core import auth as core_auth
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "voice-routes-test-master-key")
    from core import secrets_crypto
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None

    from sqlalchemy import create_engine as _ce
    from sqlalchemy.orm import sessionmaker
    from core.database import Base, SETUP_WIZARD_CRITICAL_STEPS, SetupWizardStep
    eng = _ce(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    # Mark critical wizard steps complete so the SetupGate middleware lets
    # /api/voice/* through (otherwise everything 503s).
    with SessionFactory() as db:
        for name in SETUP_WIZARD_CRITICAL_STEPS:
            db.add(SetupWizardStep(step_name=name, completed=True))
        db.commit()

    # Wire DB into the modules that hold their own SessionLocal references.
    import core.database as core_db
    from services import config_service as config_module
    monkeypatch.setattr(core_db, "SessionLocal", SessionFactory)
    monkeypatch.setattr(config_module, "SessionLocal", SessionFactory)
    monkeypatch.setattr(
        config_module, "config_service", config_module.ConfigService(db_factory=SessionFactory)
    )

    from middleware.setup_gate import _reset_cache_for_tests, refresh_setup_complete
    _reset_cache_for_tests()
    refresh_setup_complete()

    from server import app
    yield TestClient(app, headers={"Authorization": "Bearer voice-routes-test-master-key"})
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None


class TestEnginesEndpoint:
    def test_lists_all_engines(self, client):
        resp = client.get("/api/voice/engines")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Schema contract relied on by the Settings UI generic form.
        assert "tts" in body and "edge" in body["tts"]
        assert "stt" in body and "faster_whisper" in body["stt"]
        edge = body["tts"]["edge"]
        assert isinstance(edge["params"], list)
        # Voice + rate params must be exposed for the form.
        keys = {p["key"] for p in edge["params"]}
        assert {"voice", "rate"} <= keys


class TestActiveConfigEndpoint:
    def test_get_returns_defaults_when_unset(self, client):
        resp = client.get("/api/voice/active")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tts_chat"]["engine"] == "edge"
        # Stories has no engine field — locked schema.
        assert "engine" not in body["tts_stories"]

    def test_post_chat_persists_and_rejects_unknown_engine(self, client):
        bad = {"tts_chat": {"engine": "nope", "params": {}}}
        resp = client.post("/api/voice/active", json=bad)
        assert resp.status_code == 400

        good = {"tts_chat": {"engine": "edge", "params": {"voice": "vi-VN-NamMinhNeural", "rate": "+0%"}}}
        resp = client.post("/api/voice/active", json=good)
        assert resp.status_code == 200, resp.text

        # Round-trip
        body = client.get("/api/voice/active").json()
        assert body["tts_chat"]["engine"] == "edge"
        assert body["tts_chat"]["params"]["rate"] == "+0%"


class TestRequirementsEndpoint:
    def test_edge_has_no_required_binaries(self, client):
        resp = client.get("/api/voice/requirements/edge")
        assert resp.status_code == 200
        body = resp.json()
        assert body["missing_binaries"] == []
        # Edge declares no secrets either.
        assert body["secrets_present"] == {}
        assert body["ok"] is True

    def test_unknown_engine_404(self, client):
        resp = client.get("/api/voice/requirements/notreal")
        assert resp.status_code == 404


class TestSecretsEndpoint:
    def test_list_starts_empty_for_engines_with_secrets(self, client):
        # Edge isn't listed (no secrets); ElevenLabs/OpenAI/Azure all start "not set".
        body = client.get("/api/voice/secrets").json()
        assert "edge" not in body["engines"]
        assert body["engines"]["elevenlabs"] == {"api_key": False}
        assert body["engines"]["openai"] == {"api_key": False}

    def test_set_then_list_reflects_has_value(self, client):
        resp = client.post("/api/voice/secrets/elevenlabs/api_key", json={"value": "sk-secret"})
        assert resp.status_code == 200
        body = client.get("/api/voice/secrets").json()
        assert body["engines"]["elevenlabs"]["api_key"] is True

    def test_delete_clears_secret(self, client):
        client.post("/api/voice/secrets/elevenlabs/api_key", json={"value": "sk-secret"})
        resp = client.delete("/api/voice/secrets/elevenlabs/api_key")
        assert resp.status_code == 200
        body = client.get("/api/voice/secrets").json()
        assert body["engines"]["elevenlabs"]["api_key"] is False

    def test_undeclared_slot_rejected(self, client):
        # Edge declares no secrets — POST must 400, not silently accept.
        resp = client.post("/api/voice/secrets/edge/api_key", json={"value": "x"})
        assert resp.status_code == 400


class TestSTTTestEndpoint:
    def test_returns_transcript_from_warmup(self, client, monkeypatch):
        # Real faster-whisper is too heavy for unit tests; we replace
        # build_stt_service with a fake that returns a known transcript.
        from services import stt_realtime as stt_mod

        class _FakeRecorder:
            def feed_audio(self, _): pass
            def text(self): return "hello jarvis"

        class _FakeService:
            _recorder = _FakeRecorder()
            def shutdown(self): pass

        # Patch the symbol the route imports lazily.
        monkeypatch.setattr(stt_mod, "build_stt_service", lambda cfg: _FakeService())

        resp = client.post("/api/voice/test/stt")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"transcript": "hello jarvis"}


class TestEnginesVoicesEndpoint:
    def test_unknown_engine_returns_404(self, client):
        resp = client.get("/api/voice/engines/notreal/voices")
        assert resp.status_code == 404

    def test_known_non_edge_engine_returns_static_options(self, client):
        # Engines without a live probe path fall back to the registry's
        # static voice list — useful when the user is offline / has no key.
        resp = client.get("/api/voice/engines/openai/voices")
        assert resp.status_code == 200
        voices = resp.json()["voices"]
        ids = {v["id"] for v in voices}
        assert {"alloy", "echo"} <= ids
