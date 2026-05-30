"""Per-engine TTS plugins that bypass RealtimeTTS.

Each module exposes a TTSProvider subclass and a ``build_provider(params,
secrets)`` callable. The dispatcher in :mod:`services.tts_realtime` calls
it directly when the engine name does not map to a RealtimeTTS engine
class (Edge has its own optimized path; Soniox uses a hand-rolled
WebSocket client because RealtimeTTS does not ship a Soniox engine).

Feature-flag allowlist
----------------------
The ``TTS_BACKENDS_ENABLED`` env var gates which TTS engines are
buildable. Matches the STT pattern in :mod:`services.stt_backends`.

OSS-ready engines (per 2026-05-29 decision):
    * ``edge`` — free, no key required
    * ``soniox`` — paid cloud, the operator's existing daily-driver

Gated off by default (not validated for OSS):
    * ``system`` — platform TTS, behaviour varies by OS
    * ``azure`` — paid, untested
    * ``elevenlabs`` — paid, untested
    * ``openai`` — paid, untested

``tts_realtime.build_chat_provider`` consults ``assert_engine_enabled``
before dispatch; selecting a disabled engine raises ``RuntimeError``
with the env-var name in the message.
"""
from __future__ import annotations

import os


#: All TTS engines the codebase knows about. Names match what
#: ``tts_realtime.build_chat_provider`` accepts as ``config["engine"]``.
#: Edge + Soniox have hand-rolled providers (see ``tts.py`` /
#: ``tts_backends/soniox.py``); the rest go through RealtimeTTS via
#: ``_REALTIMETTS_ENGINE_CLS`` in ``tts_realtime.py``.
_KNOWN: frozenset[str] = frozenset({
    "edge",
    "soniox",
    "system",
    "azure",
    "elevenlabs",
    "openai",
})


#: Conservative OSS default — only engines validated for shipping. See
#: 2026-05-29 session. Other engines stay in ``_KNOWN`` so operators can
#: opt them in via ``TTS_BACKENDS_ENABLED``, but they're not default-on.
_DEFAULT_ENABLED: frozenset[str] = frozenset({"edge", "soniox"})


def _parse_env_list(raw: str) -> set[str]:
    return {
        s.strip() for s in (raw or "").split(",")
        if s.strip() and s.strip() in _KNOWN
    }


def _read_enabled() -> set[str]:
    """Read the current allowlist. Unset env var → conservative default
    (Edge + Soniox only). Unlike the STT side which defaults to "all
    known", TTS defaults restrict because the unselected engines depend
    on RealtimeTTS extras that may not be installed and on paid API keys
    the operator hasn't set up.
    """
    raw = os.environ.get("TTS_BACKENDS_ENABLED")
    if raw is None or raw.strip() == "":
        return set(_DEFAULT_ENABLED)
    return _parse_env_list(raw)


def list_known_engines() -> list[str]:
    return sorted(_KNOWN)


def list_enabled_engines() -> list[str]:
    return sorted(_read_enabled())


def is_engine_enabled(name: str) -> bool:
    return name in _read_enabled()


def assert_engine_enabled(name: str) -> None:
    """Raise on unknown OR disabled engines with an actionable hint."""
    if name not in _KNOWN:
        raise ValueError(
            f"Unknown TTS engine: {name!r}. "
            f"Known engines: {list_known_engines()}"
        )
    if name not in _read_enabled():
        raise RuntimeError(
            f"TTS engine {name!r} is disabled. "
            f"Currently enabled: {list_enabled_engines()}. "
            f"To enable, add it to TTS_BACKENDS_ENABLED in backend/.env "
            f"(comma-separated list)."
        )


__all__ = (
    "list_known_engines",
    "list_enabled_engines",
    "is_engine_enabled",
    "assert_engine_enabled",
)
