"""Real-import smoke test for the voice submodules.

The earlier voice e2e tests mock ``build_stt_service`` to avoid pulling
faster-whisper into CI; the side effect is that the actual
``from RealtimeSTT import AudioToTextRecorder`` line never executes
inside the test process. A regression like the cwd-shadowing namespace
package collapse (where ``backend/RealtimeSTT/`` at cwd was picked up
as an empty namespace package, hiding the editable install) slipped
past the entire suite even though manual ``Test STT`` from the dashboard
exploded immediately.

These tests do the opposite: NO mocking. They import the package the
same way the running server does — straight ``import RealtimeSTT`` /
``from RealtimeSTT import AudioToTextRecorder`` — and assert that the
spec resolves to the inner package shipped by the editable install.

If a future submodule layout change breaks resolution, this fails fast
in unit tests instead of waiting for someone to click the button.
"""
from __future__ import annotations

import pytest


class TestRealtimeSTTImport:
    def test_top_level_resolves_to_inner_package(self):
        import RealtimeSTT
        # Editable install MAPPING points at the inner package; if cwd
        # shadowing wins, __file__ is None (namespace package).
        assert RealtimeSTT.__file__ is not None, (
            "RealtimeSTT resolved to a namespace package — likely cwd "
            "shadowing via a directory name collision; rename the "
            "submodule path so it differs from the package name."
        )
        # The resolved file must live inside the editable submodule's
        # inner package, not the submodule directory itself.
        assert "/RealtimeSTT/__init__.py" in RealtimeSTT.__file__, (
            f"unexpected __file__ for RealtimeSTT: {RealtimeSTT.__file__!r}"
        )

    def test_audio_to_text_recorder_is_importable(self):
        # The exact symbol the WS path needs.
        from RealtimeSTT import AudioToTextRecorder  # noqa: F401


class TestRealtimeTTSImport:
    def test_top_level_resolves_to_inner_package(self):
        import RealtimeTTS
        assert RealtimeTTS.__file__ is not None, (
            "RealtimeTTS resolved to a namespace package — same fix as "
            "the STT counterpart."
        )
        assert "/RealtimeTTS/__init__.py" in RealtimeTTS.__file__

    def test_text_to_audio_stream_and_engines_are_importable(self):
        from RealtimeTTS import TextToAudioStream  # noqa: F401
        from RealtimeTTS import EdgeEngine, SystemEngine  # noqa: F401


class TestSherpaOnnxImport:
    """Sherpa-onnx is the runtime for the Gipformer Vietnamese backend.

    Pinned to 1.10.x because 1.12+ wheels stopped bundling
    ``libonnxruntime`` and require an exact onnxruntime ABI version
    that conflicts with what faster-whisper transitively pulls in.
    """

    def test_offline_recognizer_is_importable(self):
        import sherpa_onnx
        # Methods the Gipformer backend calls.
        assert hasattr(sherpa_onnx, "OfflineRecognizer")
        assert hasattr(sherpa_onnx.OfflineRecognizer, "from_transducer")
        assert hasattr(sherpa_onnx, "VoiceActivityDetector")
        assert hasattr(sherpa_onnx, "VadModelConfig")
        assert hasattr(sherpa_onnx, "SileroVadModelConfig")

    def test_gipformer_backend_module_imports(self):
        # Loading the backend module must not perform any network IO —
        # downloads happen lazily inside ``build()``. If module import
        # ever pulls weights, every unit test session would block on
        # slow HF downloads.
        from services.stt_backends import gipformer_vi
        assert callable(gipformer_vi.build)
        assert hasattr(gipformer_vi, "GipformerSTTService")
