"""TTS chat/stories split — code-level guarantees, not just UX hints.

The whole reason chat and stories use separate provider instances is that
a paid engine selected for chat must NOT silently consume long-form quota
when reading audiobook chapters. These tests pin down the structural
guarantees so a future refactor can't accidentally collapse them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services import shared_state
from services.tts import EdgeTTSProvider
from services.tts_realtime import build_chat_provider, build_stories_provider


ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestSharedStateSplit:
    def test_chat_and_stories_have_separate_attributes(self):
        # If these collapse to one attribute, runtime config could rebuild
        # both at once and invalidate the protection guarantee.
        assert hasattr(shared_state, "tts_chat_provider")
        assert hasattr(shared_state, "tts_stories_provider")

    def test_legacy_alias_points_at_chat(self):
        # The old `tts_provider` lives on as an alias to keep external
        # callers working — but it must never alias the stories instance,
        # or paid engines would bleed into long-form on every legacy call.
        assert shared_state.tts_provider is shared_state.tts_chat_provider


class TestStoriesFactoryLockedToEdge:
    def test_returns_edge_for_default_config(self):
        provider = build_stories_provider({})
        assert isinstance(provider, EdgeTTSProvider)

    def test_returns_edge_even_if_caller_smuggles_engine_field(self):
        # Defensive: even if voice_config validation is bypassed (direct DB
        # write, migration bug), the factory still produces Edge. This is
        # the last line of defense.
        provider = build_stories_provider({"engine": "elevenlabs", "voice": "x", "rate": "+0%"})
        assert isinstance(provider, EdgeTTSProvider)


class TestChatFactoryRouting:
    def test_edge_returns_native_provider(self):
        provider = build_chat_provider({"engine": "edge", "params": {"voice": "vi-VN-NamMinhNeural", "rate": "+0%"}})
        # Edge bypasses RealtimeTTS for the optimised tiered streaming path.
        assert isinstance(provider, EdgeTTSProvider)

    def test_unknown_engine_raises_at_construction(self):
        # Plug-and-play means: the registry is the contract. An engine name
        # not in the registry must fail loudly, not return a silent fallback
        # that pretends to work.
        with pytest.raises((ValueError, ImportError)):
            build_chat_provider({"engine": "nonexistent_engine_xyz", "params": {}})


class TestRouteDispatchSourceLevel:
    """Source-level guards on routes/tts.py — fence the dispatch pattern.

    The actual TTS path resolves which provider to use based on
    ``is_notification``. If a future edit replaces the dispatch with a raw
    ``tts_provider`` reference, both surfaces would share an engine again.
    """

    def test_routes_tts_uses_chat_provider_for_notifications(self):
        src = _read("routes/tts.py")
        assert "_state.tts_chat_provider if is_notification else _state.tts_stories_provider" in src

    def test_routes_tts_does_not_import_legacy_tts_provider_directly(self):
        src = _read("routes/tts.py")
        # The dispatch pulls from _state.* — there should be no direct
        # `from services.shared_state import tts_provider` so legacy back-
        # compat alias can't be used by mistake on this hot path.
        assert "tts_provider" not in src.split("from services.shared_state import")[1].split("\n", 1)[0]

    def test_pregen_job_still_uses_edge_directly(self):
        # Story pre-gen has always called edge_tts directly; documenting it
        # here so anyone "consolidating" through the new factory thinks twice.
        src = _read("services/tts_pregen_job.py")
        assert "import edge_tts" in src
        assert "edge_tts.Communicate" in src
