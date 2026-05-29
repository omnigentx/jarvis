"""Feature-flag tests for STT/TTS backends.

The OSS build ships with a conservative allowlist (free local STT only,
Edge + Soniox for TTS) and lets the operator opt extra backends in via
``STT_BACKENDS_ENABLED`` / ``TTS_BACKENDS_ENABLED`` env vars. These tests
pin:

* the allowlist semantics (env var → enabled set);
* the factory's "disabled backend selected → RuntimeError with actionable
  hint" contract;
* the .env.example defaults so the OSS-default never silently flips to
  include a paid backend.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════════
# STT feature flag
# ═══════════════════════════════════════════════


class TestSTTFeatureFlag:
    """Pins ``services.stt_backends`` allowlist + factory gating."""

    def test_known_backends_list(self):
        from services.stt_backends import list_known_backends
        assert set(list_known_backends()) == {
            "faster_whisper", "gipformer_vi", "soniox",
        }

    def test_default_allowlist_includes_all_known_oss_ready(self, monkeypatch):
        """All 3 STT backends are OSS-ready (per 2026-05-29 decision), so
        the default allowlist includes them all when env var is unset."""
        monkeypatch.delenv("STT_BACKENDS_ENABLED", raising=False)
        # Re-import to pick up cleared env
        import services.stt_backends as mod
        importlib.reload(mod)
        assert set(mod.list_enabled_backends()) == {
            "faster_whisper", "gipformer_vi", "soniox",
        }

    def test_env_var_restricts_allowlist(self, monkeypatch):
        monkeypatch.setenv("STT_BACKENDS_ENABLED", "faster_whisper")
        import services.stt_backends as mod
        importlib.reload(mod)
        assert mod.list_enabled_backends() == ["faster_whisper"]
        assert mod.is_backend_enabled("faster_whisper") is True
        assert mod.is_backend_enabled("soniox") is False

    def test_unknown_backend_in_env_is_filtered(self, monkeypatch):
        """Typo in env var must not crash — just gets filtered out so
        the actual known backends still work."""
        monkeypatch.setenv("STT_BACKENDS_ENABLED", "faster_whisper,bogusbackend")
        import services.stt_backends as mod
        importlib.reload(mod)
        assert "bogusbackend" not in mod.list_enabled_backends()
        assert "faster_whisper" in mod.list_enabled_backends()

    def test_disabled_backend_raises_on_build(self, monkeypatch):
        """Selecting a disabled backend in settings must fail loud at
        build time with an actionable message."""
        monkeypatch.setenv("STT_BACKENDS_ENABLED", "faster_whisper")
        import services.stt_backends as mod
        importlib.reload(mod)
        from services.stt_realtime import build_stt_service

        with pytest.raises(RuntimeError) as exc:
            build_stt_service({"backend": "soniox", "params": {}})
        msg = str(exc.value)
        assert "soniox" in msg
        assert "disabled" in msg.lower()
        assert "STT_BACKENDS_ENABLED" in msg, (
            "Error must name the env var so the operator knows where to fix"
        )

    def test_unknown_backend_raises_distinct_error(self, monkeypatch):
        """Unknown backend (typo in settings) is a separate failure mode
        from disabled — gives the right hint to the operator."""
        monkeypatch.setenv("STT_BACKENDS_ENABLED",
                           "faster_whisper,gipformer_vi,soniox")
        import services.stt_backends as mod
        importlib.reload(mod)
        from services.stt_realtime import build_stt_service

        with pytest.raises(ValueError) as exc:
            build_stt_service({"backend": "nonexistent", "params": {}})
        assert "Unknown" in str(exc.value) or "unknown" in str(exc.value)


# ═══════════════════════════════════════════════
# TTS feature flag
# ═══════════════════════════════════════════════


class TestTTSFeatureFlag:
    """Pins ``services.tts_backends`` allowlist + factory gating.

    OSS-ready as of 2026-05-29: ``edge``, ``soniox``. Other RealtimeTTS
    engines (system, azure, elevenlabs, openai) are gated off by default.
    """

    def test_known_engines_list(self):
        from services.tts_backends import list_known_engines
        assert set(list_known_engines()) == {
            "edge", "soniox", "system", "azure", "elevenlabs", "openai",
        }

    def test_default_allowlist_is_conservative(self, monkeypatch):
        """OSS default = free + tested only (edge, soniox). Paid /
        untested engines are opt-in."""
        monkeypatch.delenv("TTS_BACKENDS_ENABLED", raising=False)
        import services.tts_backends as mod
        importlib.reload(mod)
        assert set(mod.list_enabled_engines()) == {"edge", "soniox"}

    def test_env_var_can_enable_extra_engine(self, monkeypatch):
        monkeypatch.setenv("TTS_BACKENDS_ENABLED", "edge,soniox,elevenlabs")
        import services.tts_backends as mod
        importlib.reload(mod)
        assert "elevenlabs" in mod.list_enabled_engines()
        assert mod.is_engine_enabled("elevenlabs") is True

    def test_disabled_engine_raises_on_build(self, monkeypatch):
        monkeypatch.setenv("TTS_BACKENDS_ENABLED", "edge,soniox")
        import services.tts_backends as mod
        importlib.reload(mod)
        from services.tts_realtime import build_chat_provider

        with pytest.raises(RuntimeError) as exc:
            build_chat_provider({"engine": "elevenlabs", "params": {}})
        msg = str(exc.value)
        assert "elevenlabs" in msg
        assert "disabled" in msg.lower()
        assert "TTS_BACKENDS_ENABLED" in msg

    def test_edge_default_engine_remains_buildable(self, monkeypatch):
        """The default selected engine for OSS (Edge) must always be
        callable — it's the fallback when user's saved preference is
        for a disabled engine."""
        monkeypatch.delenv("TTS_BACKENDS_ENABLED", raising=False)
        import services.tts_backends as mod
        importlib.reload(mod)
        assert mod.is_engine_enabled("edge") is True


# ═══════════════════════════════════════════════
# .env.example contract
# ═══════════════════════════════════════════════


class TestEnvExampleContract:
    """The ``.env.example`` shipped with OSS sets the safe defaults that
    new operators inherit on ``cp .env.example .env``. These tests pin
    the wire so a refactor never silently exposes paid engines.
    """

    @pytest.fixture
    def env_example(self) -> str:
        path = _BACKEND_DIR / ".env.example"
        assert path.exists(), (
            ".env.example missing — required for OSS-ready feature-flag "
            "defaults. See plan in 2026-05-29 session."
        )
        return path.read_text()

    def test_stt_default_includes_all_oss_ready_backends(self, env_example):
        """All 3 STT backends are OSS-ready, so .env.example should set
        STT_BACKENDS_ENABLED to include them (operators can prune to
        local-only if they don't have a Soniox API key)."""
        assert "STT_BACKENDS_ENABLED=" in env_example
        # Pull just the value of that line (last non-comment occurrence)
        line = next(
            (l for l in env_example.splitlines()
             if l.startswith("STT_BACKENDS_ENABLED=")),
            None,
        )
        assert line is not None
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        enabled = {s.strip() for s in value.split(",")}
        assert {"faster_whisper", "gipformer_vi", "soniox"} <= enabled

    def test_tts_default_excludes_paid_untested_engines(self, env_example):
        """system / azure / elevenlabs / openai are not tested for OSS —
        must NOT appear in the default allowlist."""
        assert "TTS_BACKENDS_ENABLED=" in env_example
        line = next(
            (l for l in env_example.splitlines()
             if l.startswith("TTS_BACKENDS_ENABLED=")),
            None,
        )
        assert line is not None
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        enabled = {s.strip() for s in value.split(",")}
        for blocked in ("azure", "elevenlabs", "openai", "system"):
            assert blocked not in enabled, (
                f".env.example's TTS_BACKENDS_ENABLED must not include "
                f"{blocked!r} (not tested for OSS, see 2026-05-29)."
            )
        # Tested + ready set
        assert "edge" in enabled
        assert "soniox" in enabled
