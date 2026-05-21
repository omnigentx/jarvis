"""Runtime-config synchronisation shim.

Some startup-time state is cached in module globals and cannot be picked up
by re-reading the environment.  When the Setup Wizard or Settings UI writes
a new value to the DB, those caches need to be refreshed in-process or the
rest of the code keeps using the stale copy.

Two entry points are provided:

* **Imperative apply_*** functions.  Callable directly from routes that just
  wrote a value and want the change to take effect inside the same request
  (e.g. the master key rotation in ``routes/settings.py``).
* **register_config_listeners()**.  Subscribes to
  :class:`~services.config_service.ConfigService` change events and fans
  them out to the matching ``apply_*`` function.  Wired once at app
  startup from ``server.py`` so changes made anywhere in the codebase —
  including the Setup Wizard, bulk updates, or direct ``config_service.set``
  calls from other services — hot-reload without the caller having to know
  about this module.

D2 scope (Phase 3a) covers:

* ``auth/JARVIS_API_KEY``         — auth password rotation (no crypto impact)
* ``system/LOG_CONSOLE_LEVEL``    — console log verbosity
* ``voice/tts.chat``              — rebuild chat TTS provider (registry JSON)
* ``voice/tts.stories``           — rebuild stories TTS provider (Edge schema)
* ``voice/stt``                   — rebuild STT recorder
* ``voice/secrets.{engine}.{slot}`` — encrypted API keys

Values that require a full restart (e.g. ``system/SESSION_HISTORY_WINDOW``
and ``system/CORS_ORIGINS``) are intentionally *not* wired here; the UI
surfaces a "Requires Restart" pill instead of pretending the change is
live.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Callable, Optional

from core import auth as core_auth
from core import secrets_crypto
from services.llm_provider_sync import apply_llm_provider_change, parse_llm_key

logger = logging.getLogger(__name__)


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"

# Service config keys that look like env var names (UPPER_SNAKE_CASE, starting
# with a letter) are exported to ``os.environ`` so MCP subprocesses spawned
# later inherit them.  Keeps the wizard's "Services" step useful end-to-end:
# fill the form → restart the relevant tool subprocess → it picks up the value.
_ENV_SHAPED_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


# ---- Imperative apply_* -----------------------------------------------------


def apply_api_key(api_key: str) -> None:
    """Propagate a new ``JARVIS_API_KEY`` (auth/web password) to env + the
    auth module's cached global.

    Crypto is *not* touched here — that's the whole point of the
    ``JARVIS_API_KEY`` ↔ ``JARVIS_MASTER_KEY`` split. Rotating the auth
    password is a routine operator action; rotating crypto requires
    re-encrypting the DB and is done via the rotate-master-key CLI.
    """
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("api_key must be a non-empty string")

    os.environ["JARVIS_API_KEY"] = api_key
    # ``core.auth`` reads its module-global inside the verify dependency at
    # call-time, so mutating the attribute is enough.
    core_auth.JARVIS_API_KEY = api_key
    logger.info("[RUNTIME] API/auth key applied")


def apply_master_key(master_key: str) -> str:
    """Propagate a new ``JARVIS_MASTER_KEY`` to env and force crypto reload.

    Only call this AFTER re-encrypting the DB under the new key (use
    ``scripts/rotate_master_key.py``). Calling it on a DB still encrypted
    under the old key renders every stored secret undecryptable.

    Returns the new Fernet fingerprint for diagnostic logging.
    """
    if not isinstance(master_key, str) or not master_key.strip():
        raise ValueError("master_key must be a non-empty string")

    os.environ["JARVIS_MASTER_KEY"] = master_key
    fingerprint = secrets_crypto.reload_master_key()
    logger.info("[RUNTIME] Master key applied (fingerprint=%s)", fingerprint)
    return fingerprint


def apply_log_console_level(level: Optional[str]) -> str:
    """Retune the console handler attached to the root logger.

    ``level=None`` falls back to ``WARNING`` — the historical default from
    :mod:`core.logging_config`.  The file handler's level is left untouched
    so on-disk logs keep their full resolution regardless of UI changes.
    """
    normalised = (level or "WARNING").strip().upper()
    if normalised not in _VALID_LOG_LEVELS:
        raise ValueError(
            f"LOG_CONSOLE_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}; got {level!r}"
        )

    os.environ["LOG_CONSOLE_LEVEL"] = normalised
    numeric = getattr(logging, normalised)
    root = logging.getLogger()
    touched = 0
    for handler in root.handlers:
        # Only the pure StreamHandler (stdout) — FileHandler and its
        # rotating subclass inherit from StreamHandler so we must exclude
        # them explicitly.
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            handler.setLevel(numeric)
            touched += 1
    if touched == 0:
        logger.debug("[RUNTIME] No console handler found; level %s stored in env only", normalised)
    else:
        logger.info("[RUNTIME] Console log level set to %s (%d handler(s))", normalised, touched)
    return normalised


def apply_timezone(tz: Optional[str]) -> str:
    """Validate and set JARVIS_TIMEZONE in env.

    MCP subprocesses already running under fast-agent use stdio transport
    and cannot be reconnected after a kill/respawn.  The new timezone takes
    effect for any subprocess spawned *after* this call — in practice, on
    the next backend restart.  The UI shows a "Requires Restart" pill to
    communicate this constraint.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    normalised = (tz or _DEFAULT_TIMEZONE).strip()
    try:
        ZoneInfo(normalised)
    except ZoneInfoNotFoundError:
        raise ValueError(f"Unknown timezone: {normalised!r}") from None
    os.environ["JARVIS_TIMEZONE"] = normalised
    logger.info("[RUNTIME] Timezone set to %s — restart backend for MCP tools to pick up", normalised)
    return normalised


def _get_config_service():
    """Module-level singleton from services.config_service — single source of truth."""
    from services.config_service import config_service
    return config_service


def apply_voice_chat_config(config: Optional[dict] = None) -> str:
    """Rebuild the chat TTS provider from the registry-driven JSON config.

    ``config`` is the parsed ``voice.tts.chat`` value. When ``None``, the
    function reads from :mod:`services.config_service` itself.
    """
    from services import shared_state
    from services.tts_realtime import build_chat_provider
    from services import voice_config as _vc

    cs = _get_config_service()
    if config is None:
        config = _vc.get_chat_config(cs)
    secrets_for_engine = _vc.get_engine_secrets(cs, config.get("engine", "edge"))

    new_provider = build_chat_provider(config, secrets={config.get("engine", "edge"): secrets_for_engine})
    shared_state.tts_chat_provider = new_provider
    logger.info(
        "[RUNTIME] Chat TTS provider rebuilt: engine=%s class=%s",
        config.get("engine"),
        type(new_provider).__name__,
    )
    return type(new_provider).__name__


def apply_voice_stories_config(config: Optional[dict] = None) -> str:
    """Rebuild the stories TTS provider — always Edge, by design."""
    from services import shared_state
    from services.tts_realtime import build_stories_provider
    from services import voice_config as _vc

    if config is None:
        config = _vc.get_stories_config(_get_config_service())

    new_provider = build_stories_provider(config)
    shared_state.tts_stories_provider = new_provider
    logger.info(
        "[RUNTIME] Stories TTS provider rebuilt: voice=%s rate=%s",
        config.get("voice"),
        config.get("rate"),
    )
    return type(new_provider).__name__


def apply_voice_stt_config(config: Optional[dict] = None) -> str:
    """Rebuild the STT recorder from the registry-driven JSON config.

    The recorder is heavy (spawns worker procs for faster-whisper); we lazily
    create it on first need and tear down the old one on swap.
    """
    from services import shared_state
    from services import voice_config as _vc

    if config is None:
        config = _vc.get_stt_config(_get_config_service())

    # Lazy import — STT pulls torch/faster-whisper which is heavy.
    from services.stt_realtime import build_stt_service

    old = shared_state.stt_recorder
    new_service = build_stt_service(config)
    shared_state.stt_recorder = new_service
    if old is not None:
        try:
            old.shutdown()
        except Exception:
            logger.exception("[RUNTIME] failed to shut down old STT recorder")
    logger.info(
        "[RUNTIME] STT service rebuilt: backend=%s wake_word=%s",
        config.get("backend"),
        (config.get("wake_word") or {}).get("backend"),
    )
    return "stt_ready"


# ---- Listener bridge --------------------------------------------------------


def _on_config_change(event) -> None:
    """Dispatch a :class:`ConfigChangeEvent` to the right ``apply_*``.

    Deletes restore the default (or clear the env var) so a user who
    removes an override gets the built-in behaviour back without needing
    to restart.
    """
    cat, key, new_value, action = (
        event.category,
        event.key,
        event.new_value,
        event.action,
    )

    try:
        if cat == "auth" and key == "JARVIS_API_KEY":
            if action == "delete":
                logger.warning("[RUNTIME] API key deleted — leaving in-process copy untouched")
                return
            if new_value:
                apply_api_key(new_value)
            return

        if cat == "system":
            if key == "LOG_CONSOLE_LEVEL":
                apply_log_console_level(new_value if action != "delete" else None)
                return
            if key == "TIMEZONE":
                apply_timezone(new_value if action != "delete" else None)
                return

        if cat == "voice":
            # JSON-driven voice config — single source of truth for chat/stories/stt.
            # Keys: "tts.chat", "tts.stories", "stt", "secrets.{engine}.{slot}"
            import json as _json
            if key == "tts.chat":
                cfg = _json.loads(new_value) if (action != "delete" and new_value) else None
                apply_voice_chat_config(cfg)
                return
            if key == "tts.stories":
                cfg = _json.loads(new_value) if (action != "delete" and new_value) else None
                apply_voice_stories_config(cfg)
                return
            if key == "stt":
                cfg = _json.loads(new_value) if (action != "delete" and new_value) else None
                apply_voice_stt_config(cfg)
                return
            if key.startswith("secrets."):
                # Secret rotation: rebuild chat provider so new key is picked up.
                # (Stories never uses secrets — Edge has none.)
                apply_voice_chat_config(None)
                return

        if cat == "llm":
            parsed = parse_llm_key(key)
            if parsed is not None:
                provider, kind = parsed
                apply_llm_provider_change(provider, kind, new_value, action=action)
                return

        if cat == "service.github":
            # GitHub service fields (personal_access_token, user_name, user_email)
            # don't match the ENV_SHAPED_KEY pattern — they drive file sinks
            # (git-credentials + gitconfig + fastagent.secrets.yaml + .gh-config/hosts.yml)
            # instead of os.environ. Must run before the generic service.* env branch.
            from services import git_credential_sync, gh_credential_sync
            git_credential_sync.apply_change(key, new_value, action=action)
            gh_credential_sync.apply_change(key, new_value, action=action)
            return

        if cat.startswith("service.") and _ENV_SHAPED_KEY_RE.match(key):
            if action == "delete":
                os.environ.pop(key, None)
                logger.info("[RUNTIME] Unset %s (service env)", key)
            elif new_value is not None:
                os.environ[key] = str(new_value)
                logger.info("[RUNTIME] Set %s (service env, %d chars)", key, len(str(new_value)))
            return
    except Exception:
        # A listener that raises would be caught by ConfigService._emit, but
        # we log here too so the failure is obvious in context.
        logger.exception(
            "[RUNTIME] Failed to apply runtime change for %s/%s", cat, key
        )


def reconcile_service_env(service) -> int:
    """Seed ``os.environ`` from every ``service.*/{UPPER_SNAKE}`` row in DB.

    The change-listener keeps env in sync when rows are *mutated* at runtime,
    but a fresh backend process (Docker restart, CLI relaunch) starts with
    an empty ``os.environ`` and no "change" ever fires for rows that were
    written in a previous run. Without this boot-time seed, MCP subprocesses
    spawned by fast-agent immediately after startup inherit a stale/empty
    env even though the DB carries perfectly good credentials.

    Only keys shaped like ``UPPER_SNAKE_CASE`` are exported — the same
    filter the change-listener uses — so free-form service keys stay in
    DB-only mode.

    Returns the number of env entries populated, so the caller can log a
    clear audit line at startup.
    """
    exported = 0
    skipped: list[str] = []
    for category, entries in service.list_all().items():
        if not category.startswith("service."):
            continue
        for entry in entries:
            if not _ENV_SHAPED_KEY_RE.match(entry.key):
                continue
            # Tolerate per-secret decrypt failures here. config_service.get
            # is fail-closed (raises DecryptError on InvalidToken) because a
            # *runtime* caller depends on the secret being correct. Bootstrap
            # is fan-out: one stale row encrypted under a rotated master key
            # must not crash the whole backend — that would brick the deploy
            # for a feature the user may not even use. Skip + warn instead;
            # the user re-sets via Settings later when they hit the feature.
            try:
                plaintext = service.get(category, entry.key)
            except secrets_crypto.DecryptError as exc:
                skipped.append(f"{category}/{entry.key}")
                logger.warning(
                    "[BOOTSTRAP] Skipped %s/%s: %s", category, entry.key, exc,
                )
                continue
            if plaintext is None or plaintext == "":
                continue
            # Respect env that was set explicitly outside the DB (e.g. via
            # docker-compose ``environment:``) — those represent a deliberate
            # deployment override, so we shouldn't clobber them.
            if os.environ.get(entry.key):
                continue
            os.environ[entry.key] = str(plaintext)
            exported += 1
            logger.info(
                "[BOOTSTRAP] Seeded %s from %s (%d chars)",
                entry.key, category, len(str(plaintext)),
            )
    if skipped:
        logger.warning(
            "[BOOTSTRAP] %d secret(s) undecryptable and skipped: %s. "
            "Re-set via Settings → Services to restore.",
            len(skipped), ", ".join(skipped),
        )
    return exported


def register_config_listeners(service) -> Callable[[], None]:
    """Wire hot-reload dispatch to the given :class:`ConfigService`.

    Returns the service's unsubscribe callback so tests can detach cleanly.
    Safe to call multiple times — the dispatcher is idempotent per change
    (it only mutates state that reflects the new value anyway).
    """
    unsubscribe = service.subscribe(_on_config_change)
    logger.info("[RUNTIME] Config change listeners registered")
    return unsubscribe
