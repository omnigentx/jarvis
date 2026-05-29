"""Conformance tests for the STT backend Protocol.

Every backend that gets registered in
``services.stt_realtime._BACKEND_FACTORIES`` MUST implement
``services.stt_backends.types.STTServiceProtocol``. The duck-typed surface
(``feed_audio``, ``set_hook``, …, plus the new ``pause`` / ``resume`` /
``is_alive`` / ``connection_state``) is what ``routes/ws_voice.py`` depends
on — adding a backend that's missing any of these methods silently breaks
the mic-driven lifecycle ([[ws lifecycle fix 2026-05-29]]).

These tests fail loud at CI time so the regression doesn't surface as
"page reload → mic không phản hồi" 9 hours later.
"""
from __future__ import annotations

import importlib

import pytest

from services.stt_backends.types import STTConnectionState, STTServiceProtocol


# (module_path, class_name) for every concrete STT backend in the registry.
# Imports are deferred to the test body so heavy dependencies (torch,
# sherpa-onnx) only load when their backend is actually exercised.
_BACKENDS: list[tuple[str, str]] = [
    ("services.stt_backends._ws_streaming", "WSStreamingSTT"),
    ("services.stt_realtime", "RealtimeSTTService"),
    ("services.stt_backends.soniox", "SonioxSTTService"),
    ("services.stt_backends.gipformer_vi", "GipformerSTTService"),
]


def _resolve(module_path: str, class_name: str):
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name, None)
    assert cls is not None, f"{class_name} not found in {module_path}"
    return cls


@pytest.mark.parametrize("module_path,class_name", _BACKENDS)
def test_backend_has_protocol_methods(module_path: str, class_name: str):
    """Each backend MUST expose every method named in STTServiceProtocol."""
    cls = _resolve(module_path, class_name)
    required_methods = (
        "feed_audio",
        "set_hook",
        "start_listen_loop",
        "resume",
        "pause",
        "shutdown",
    )
    missing = [m for m in required_methods if not callable(getattr(cls, m, None))]
    assert not missing, (
        f"{class_name} missing methods: {missing}. "
        f"Every STT backend must implement STTServiceProtocol — see "
        f"services/stt_backends/types.py."
    )


@pytest.mark.parametrize("module_path,class_name", _BACKENDS)
def test_backend_has_state_properties(module_path: str, class_name: str):
    """Each backend MUST expose ``is_alive`` and ``connection_state`` so the
    route can probe health and forward state to the frontend without
    special-casing per provider."""
    cls = _resolve(module_path, class_name)
    for prop in ("is_alive", "connection_state"):
        assert hasattr(cls, prop), (
            f"{class_name} missing property {prop!r}. "
            f"Required by STTServiceProtocol for ws_status forwarding."
        )


def test_state_enum_values_are_stable_wire_format():
    """Wire format pin: enum values are the strings the frontend chip
    renders against. Changing them silently breaks the UI mapping.
    """
    assert STTConnectionState.IDLE.value == "idle"
    assert STTConnectionState.CONNECTING.value == "connecting"
    assert STTConnectionState.CONNECTED.value == "connected"
    assert STTConnectionState.RECONNECTING.value == "reconnecting"
    assert STTConnectionState.ERROR.value == "error"


def test_ws_streaming_base_implements_protocol():
    """Runtime-checkable Protocol guard — subclass instances satisfy
    isinstance check. Catches silent removal of a method that's still
    declared on the class statement but stubbed out."""
    from services.stt_backends._ws_streaming import WSStreamingSTT

    class _Probe(WSStreamingSTT):
        WS_URL = "wss://localhost:1/__probe__"
        LOG_TAG = "Probe"

        def _build_config_message(self):
            return {}

        def _handle_event(self, data):
            pass

    inst = _Probe()
    assert isinstance(inst, STTServiceProtocol), (
        "WSStreamingSTT subclass must satisfy runtime_checkable "
        "STTServiceProtocol"
    )
    # Initial state machine invariants
    assert inst.connection_state == STTConnectionState.IDLE
    assert not inst.is_alive  # No thread started yet
