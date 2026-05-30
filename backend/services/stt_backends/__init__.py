"""Per-backend STT plugins.

Each module in this package exposes a top-level ``build(config)`` callable
that returns an STT service object satisfying
:class:`services.stt_backends.types.STTServiceProtocol` (feed_audio,
set_hook, start_listen_loop, resume, pause, shutdown, plus the on_*
callback methods and is_alive / connection_state properties).

The dispatcher in :mod:`services.stt_realtime` looks up the right
backend module by ``config["backend"]`` and calls its ``build(config)``.
Adding a new engine is a 2-touch change: add a registry entry plus a
new module here. Nothing in ``ws_voice`` or ``runtime_config`` needs to
care which backend produced the service.

Feature-flag allowlist
----------------------
The ``STT_BACKENDS_ENABLED`` env var gates which backends are buildable.
Useful for OSS distributions that ship with a conservative default and
let advanced users opt extra backends in.

Format: comma-separated list. Unknown names are silently filtered so a
typo doesn't crash boot. When the env var is unset, ALL known backends
are enabled — the dev-machine default; OSS ``.env.example`` writes an
explicit value so the default never silently flips to include something
operators haven't opted into.

``stt_realtime.build_stt_service`` consults ``assert_backend_enabled``
before dispatch; selecting a disabled backend raises ``RuntimeError``
with the env-var name in the message so the operator knows exactly
where to fix it.
"""
from __future__ import annotations

import os


#: All STT backends shipped in this codebase. Keep in sync with
#: ``services.stt_realtime._BACKEND_FACTORIES``. Conformance test in
#: ``tests/test_stt_protocol_conformance.py`` pins each entry to a class
#: that implements ``STTServiceProtocol``.
_KNOWN: frozenset[str] = frozenset({
    "faster_whisper",
    "gipformer_vi",
    "soniox",
})


def _parse_env_list(raw: str) -> set[str]:
    """Split a comma-separated env value and filter to known backends."""
    return {
        s.strip() for s in (raw or "").split(",")
        if s.strip() and s.strip() in _KNOWN
    }


def _read_enabled() -> set[str]:
    """Read the current allowlist from the env var. Re-read on every
    call so monkeypatched env in tests is honoured without module reload.

    Unset env var → all known backends enabled (dev default). OSS
    ``.env.example`` writes an explicit value so a fresh install never
    silently inherits a flag-state the operator didn't choose.
    """
    raw = os.environ.get("STT_BACKENDS_ENABLED")
    if raw is None or raw.strip() == "":
        return set(_KNOWN)
    return _parse_env_list(raw)


def list_known_backends() -> list[str]:
    """All backend ids the codebase knows about, sorted."""
    return sorted(_KNOWN)


def list_enabled_backends() -> list[str]:
    """Subset currently enabled per the feature-flag env var, sorted."""
    return sorted(_read_enabled())


def is_backend_enabled(name: str) -> bool:
    """True iff the backend is both known AND in the current allowlist."""
    return name in _read_enabled()


def assert_backend_enabled(name: str) -> None:
    """Raise on unknown OR disabled backends with an actionable hint.

    Called by ``build_stt_service`` before dispatch. The message names
    the env var explicitly so the operator can fix config without
    spelunking the registry.
    """
    if name not in _KNOWN:
        raise ValueError(
            f"Unknown STT backend: {name!r}. "
            f"Known backends: {list_known_backends()}"
        )
    if name not in _read_enabled():
        raise RuntimeError(
            f"STT backend {name!r} is disabled. "
            f"Currently enabled: {list_enabled_backends()}. "
            f"To enable, add it to STT_BACKENDS_ENABLED in backend/.env "
            f"(comma-separated list)."
        )


__all__ = (
    "list_known_backends",
    "list_enabled_backends",
    "is_backend_enabled",
    "assert_backend_enabled",
)
