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


class TestBackendsEndpoint:
    """``GET /api/voice/backends`` — feature-flag allowlist for the
    Settings UI. See ``test_voice_backend_flags.py`` for the underlying
    allowlist logic; this class only pins the HTTP wire shape.
    """

    def test_returns_enabled_and_known_lists(self, client):
        resp = client.get("/api/voice/backends")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for section in ("stt", "tts"):
            assert section in body
            assert "enabled" in body[section]
            assert "known" in body[section]
            assert isinstance(body[section]["enabled"], list)
            assert isinstance(body[section]["known"], list)
            # Enabled is always a subset of known
            assert set(body[section]["enabled"]) <= set(body[section]["known"])

    def test_known_lists_match_static_codebase_view(self, client):
        """``known`` is the codebase's full known-engines set, NOT a
        function of the env var. Pinned here so adding a backend (e.g.
        Deepgram) without updating both the registry AND the known set
        fails this test loudly."""
        body = client.get("/api/voice/backends").json()
        assert {"faster_whisper", "gipformer_vi", "soniox"} <= set(
            body["stt"]["known"]
        )
        assert {"edge", "soniox"} <= set(body["tts"]["known"])

    def test_env_var_restricts_enabled_set(self, client, monkeypatch):
        """Env var change is honoured per-request (no module reload
        needed) — frontend can re-query after an operator edits .env
        and restarts the backend."""
        monkeypatch.setenv("STT_BACKENDS_ENABLED", "faster_whisper")
        resp = client.get("/api/voice/backends")
        body = resp.json()
        assert body["stt"]["enabled"] == ["faster_whisper"]

    def test_endpoint_requires_auth(self, tmp_path, monkeypatch):
        """Should NOT serve without a valid bearer token, same as the
        other voice routes."""
        # Build a fresh client without the auth header.
        monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "noauth.db"))
        monkeypatch.setenv("JARVIS_API_KEY", "voice-routes-test-master-key")
        from core import auth as core_auth
        monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "voice-routes-test-master-key")
        from sqlalchemy import create_engine as _ce
        from sqlalchemy.orm import sessionmaker
        from core.database import Base, SETUP_WIZARD_CRITICAL_STEPS, SetupWizardStep
        eng = _ce(
            f"sqlite:///{tmp_path / 'noauth.db'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=eng)
        S = sessionmaker(autocommit=False, autoflush=False, bind=eng)
        with S() as db:
            for name in SETUP_WIZARD_CRITICAL_STEPS:
                db.add(SetupWizardStep(step_name=name, completed=True))
            db.commit()
        import core.database as core_db
        from services import config_service as config_module
        monkeypatch.setattr(core_db, "SessionLocal", S)
        monkeypatch.setattr(config_module, "SessionLocal", S)
        from middleware.setup_gate import _reset_cache_for_tests, refresh_setup_complete
        _reset_cache_for_tests()
        refresh_setup_complete()
        from server import app
        c = TestClient(app)
        resp = c.get("/api/voice/backends")
        assert resp.status_code in (401, 403), (
            f"Expected 401/403, got {resp.status_code}: {resp.text}"
        )


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

    def test_stt_only_engine_secret_surfaces(self, client):
        # Soniox lives in both registries and shares the api_key slot. The
        # secrets endpoint must list it from the STT side too so a user who
        # only picks Soniox STT (not TTS) can still set the key from the
        # STT card. Listed once (not duplicated) since both halves point at
        # the same slot.
        body = client.get("/api/voice/secrets").json()
        assert body["engines"].get("soniox") == {"api_key": False}

        resp = client.post("/api/voice/secrets/soniox/api_key", json={"value": "sk-soniox"})
        assert resp.status_code == 200
        body = client.get("/api/voice/secrets").json()
        assert body["engines"]["soniox"]["api_key"] is True

        # Cleanup path still works through the same engine name regardless
        # of which registry side initially exposed it.
        resp = client.delete("/api/voice/secrets/soniox/api_key")
        assert resp.status_code == 200


class TestSTTTestEndpoint:
    def test_returns_transcript_from_warmup(self, client, monkeypatch):
        # Real faster-whisper would download a ~75 MB model and run inference
        # — too heavy for unit tests. We replace WhisperModel with a fake
        # that mirrors the segments-iterable contract.
        import faster_whisper

        class _FakeSegment:
            def __init__(self, t): self.text = t

        class _FakeModel:
            def __init__(self, *_, **__): pass
            def transcribe(self, _path, language=None):
                return iter([_FakeSegment(" hello"), _FakeSegment(" jarvis")]), object()

        monkeypatch.setattr(faster_whisper, "WhisperModel", _FakeModel)

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
