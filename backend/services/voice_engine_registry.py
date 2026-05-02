"""Voice engine registry — single source of truth for STT/TTS plug-and-play.

Each entry declares:
  * the importable class (lazy-loaded)
  * the user-facing parameters with type/default/options metadata so the UI
    can render a form without knowing about each engine
  * declared system requirements (binaries, API keys) so the wizard can
    surface "missing prerequisites" hints
  * declared secret keys so the API layer never leaks plaintext

Adding a new engine = adding one entry here. The Setup Wizard, Settings tab,
factory, and runtime_config dispatcher all read from this dict — no other
file needs editing for plug-and-play to extend.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict


ParamType = Literal["select", "text", "number", "slider", "toggle", "secret"]


class ParamSpec(TypedDict, total=False):
    key: str
    type: ParamType
    label: str
    help: str
    default: Any
    options: list[Any]
    min: float
    max: float
    step: float


class TTSEngineSpec(TypedDict, total=False):
    label: str
    description: str
    badges: list[str]
    requires: list[str]
    secrets: list[str]
    params: list[ParamSpec]
    realtimetts_engine: str   # class name in RealtimeTTS module
    output_format: Literal["mp3", "pcm"]


class STTBackendSpec(TypedDict, total=False):
    label: str
    description: str
    badges: list[str]
    params: list[ParamSpec]
    wake_word_backends: dict[str, dict[str, Any]]


# Voice picker for Edge defaults to common vi/en voices. The list is intentionally
# short — a "Refresh from server" button on the UI calls /api/voice/engines/edge/voices
# to populate the full ~400-voice catalog dynamically.
_EDGE_DEFAULT_VOICES = [
    "vi-VN-NamMinhNeural",
    "vi-VN-HoaiMyNeural",
    "en-US-AriaNeural",
    "en-US-GuyNeural",
    "en-US-JennyNeural",
]


TTS_ENGINES: dict[str, TTSEngineSpec] = {
    "edge": {
        "label": "Microsoft Edge TTS",
        "description": "Free, no API key, native vi-VN + en-US voices. Recommended default.",
        "badges": ["free", "cloud", "vi+en"],
        "requires": [],
        "secrets": [],
        "realtimetts_engine": "EdgeEngine",
        "output_format": "mp3",
        "params": [
            {
                "key": "voice",
                "type": "select",
                "label": "Voice",
                "default": "vi-VN-NamMinhNeural",
                "options": _EDGE_DEFAULT_VOICES,
                "help": "Click 'Refresh voices' in UI to fetch the full catalog.",
            },
            {
                "key": "rate",
                "type": "text",
                "label": "Speech rate",
                "default": "+20%",
                "help": "edge-tts rate string, e.g. '+0%', '+20%', '-10%'.",
            },
        ],
    },
    "system": {
        "label": "System TTS (pyttsx3)",
        "description": "Built-in OS voice. No network. Quality varies by platform.",
        "badges": ["free", "local", "offline"],
        "requires": [],
        "secrets": [],
        "realtimetts_engine": "SystemEngine",
        "output_format": "pcm",
        "params": [
            {"key": "voice", "type": "text", "label": "Voice id", "default": "", "help": "Leave empty for system default."},
            {"key": "rate", "type": "number", "label": "Rate (wpm)", "default": 200, "min": 50, "max": 400, "step": 10},
        ],
    },
    "azure": {
        "label": "Azure Speech",
        "description": "500+ voices, high quality. Free tier 500k chars/month.",
        "badges": ["cloud", "high-quality"],
        "requires": [],
        "secrets": ["api_key", "region"],
        "realtimetts_engine": "AzureEngine",
        "output_format": "mp3",
        "params": [
            {"key": "voice", "type": "text", "label": "Voice", "default": "vi-VN-NamMinhNeural"},
        ],
    },
    "elevenlabs": {
        "label": "ElevenLabs",
        "description": "Premium quality, supports vi+en code-switch in one sentence.",
        "badges": ["cloud", "paid", "code-switch"],
        "requires": ["mpv"],
        "secrets": ["api_key"],
        "realtimetts_engine": "ElevenlabsEngine",
        "output_format": "mp3",
        "params": [
            {"key": "voice", "type": "text", "label": "Voice id or name", "default": "Rachel"},
            {"key": "model", "type": "select", "label": "Model", "default": "eleven_multilingual_v2", "options": ["eleven_multilingual_v2", "eleven_turbo_v2", "eleven_monolingual_v1"]},
        ],
    },
    "openai": {
        "label": "OpenAI TTS",
        "description": "6 voices, multilingual, premium pricing.",
        "badges": ["cloud", "paid"],
        "requires": ["ffmpeg"],
        "secrets": ["api_key"],
        "realtimetts_engine": "OpenAIEngine",
        "output_format": "mp3",
        "params": [
            {"key": "voice", "type": "select", "label": "Voice", "default": "alloy", "options": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]},
            {"key": "model", "type": "select", "label": "Model", "default": "tts-1", "options": ["tts-1", "tts-1-hd"]},
        ],
    },
}


STT_BACKENDS: dict[str, STTBackendSpec] = {
    "faster_whisper": {
        "label": "faster-whisper (local)",
        "description": "Local inference via faster-whisper. No API key. CPU works on tiny/base.",
        "badges": ["free", "local", "vi+en"],
        # Defaults mirror RealtimeVoiceChat's DEFAULT_RECORDER_CONFIG (the
        # author's own voice-chat reference impl). silero_sensitivity 0.05 is
        # the critical one: 0.4 (Silero default) was strict enough that
        # AirPods-level speech (peak ~1200) didn't trigger VAD at all.
        "params": [
            {"key": "model", "type": "select", "label": "Final model", "default": "base", "options": ["tiny", "base", "small", "medium", "large-v2", "large-v3"]},
            {"key": "realtime_model_type", "type": "select", "label": "Realtime model", "default": "base", "options": ["tiny", "base", "small"]},
            {"key": "language", "type": "select", "label": "Language", "default": "auto", "options": ["auto", "vi", "en"]},
            {"key": "compute_type", "type": "select", "label": "Compute type", "default": "int8", "options": ["int8", "float16", "float32"]},
            {"key": "silero_sensitivity", "type": "slider", "label": "VAD sensitivity (Silero)", "default": 0.05, "min": 0, "max": 1, "step": 0.05},
            {"key": "webrtc_sensitivity", "type": "number", "label": "VAD sensitivity (WebRTC, 0-3)", "default": 3, "min": 0, "max": 3, "step": 1},
            {"key": "post_speech_silence_duration", "type": "number", "label": "End-of-speech silence (s)", "default": 0.7, "min": 0.2, "max": 3.0, "step": 0.1},
            {"key": "min_length_of_recording", "type": "number", "label": "Min recording length (s)", "default": 0.5, "min": 0.0, "max": 3.0, "step": 0.1},
            {"key": "min_gap_between_recordings", "type": "number", "label": "Min gap between recordings (s)", "default": 0, "min": 0.0, "max": 3.0, "step": 0.1},
            {"key": "beam_size", "type": "number", "label": "Beam size (final)", "default": 3, "min": 1, "max": 10},
            {"key": "beam_size_realtime", "type": "number", "label": "Beam size (realtime)", "default": 3, "min": 1, "max": 10},
            {"key": "realtime_processing_pause", "type": "number", "label": "Realtime processing pause (s)", "default": 0.03, "min": 0.0, "max": 1.0, "step": 0.01},
            {"key": "enable_realtime_transcription", "type": "toggle", "label": "Stream partial transcripts", "default": True},
            {"key": "silero_deactivity_detection", "type": "toggle", "label": "Silero deactivity detection (better end-of-speech)", "default": True},
            {"key": "silero_use_onnx", "type": "toggle", "label": "Silero ONNX (faster CPU inference)", "default": True},
            {"key": "faster_whisper_vad_filter", "type": "toggle", "label": "faster-whisper internal VAD filter", "default": False},
            {"key": "allowed_latency_limit", "type": "number", "label": "Allowed latency limit (chunks)", "default": 500, "min": 50, "max": 5000, "step": 50},
        ],
        "wake_word_backends": {
            "off": {"label": "Disabled", "params": []},
            "porcupine": {
                "label": "Picovoice Porcupine",
                "params": [
                    {"key": "wake_words", "type": "text", "label": "Wake word(s)", "default": "jarvis", "help": "Comma-separated."},
                    {"key": "wake_words_sensitivity", "type": "slider", "label": "Sensitivity", "default": 0.6, "min": 0, "max": 1, "step": 0.05},
                ],
                "secrets": ["access_key"],
            },
            "oww": {
                "label": "OpenWakeWord",
                "params": [
                    {"key": "wake_words", "type": "text", "label": "Wake word(s)", "default": "jarvis"},
                    {"key": "wake_words_sensitivity", "type": "slider", "label": "Sensitivity", "default": 0.6, "min": 0, "max": 1, "step": 0.05},
                ],
                "secrets": [],
            },
        },
    },
}


def list_tts_engines() -> dict[str, TTSEngineSpec]:
    return TTS_ENGINES


def list_stt_backends() -> dict[str, STTBackendSpec]:
    return STT_BACKENDS


def get_tts_engine(name: str) -> Optional[TTSEngineSpec]:
    return TTS_ENGINES.get(name)


def get_stt_backend(name: str) -> Optional[STTBackendSpec]:
    return STT_BACKENDS.get(name)


def default_tts_chat_config() -> dict[str, Any]:
    """First-run / fallback chat TTS config. Edge default per onboarding rule."""
    return {
        "engine": "edge",
        "params": {p["key"]: p.get("default") for p in TTS_ENGINES["edge"]["params"]},
    }


def default_tts_stories_config() -> dict[str, Any]:
    """Stories TTS — locked schema (no engine field; always Edge)."""
    edge_params = {p["key"]: p.get("default") for p in TTS_ENGINES["edge"]["params"]}
    return edge_params


def default_stt_config() -> dict[str, Any]:
    spec = STT_BACKENDS["faster_whisper"]
    return {
        "backend": "faster_whisper",
        "params": {p["key"]: p.get("default") for p in spec["params"]},
        "wake_word": {"backend": "off", "params": {}},
    }
