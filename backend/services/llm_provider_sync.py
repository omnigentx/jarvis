"""LLM provider config ↔ fast-agent bridge.

Jarvis stores per-provider credentials (api_key + base_url) in the DB under the
``llm`` category.  Fast-agent, however, resolves credentials in two ways:

* **Env vars** (``{PROVIDER}_API_KEY``) — used as a fallback when ``{provider}.api_key``
  is missing from the YAML config.
* **YAML sections** (``openai.base_url`` / ``anthropic.api_key`` / ``generic.*``) —
  the only way fast-agent picks up a custom base URL; there is no env fallback
  for base URLs.

This module translates between Jarvis's DB keys and both surfaces so a change
in Settings takes effect for fast-agent-spawned subprocesses on their next
launch without requiring a full backend restart.

DB key convention
-----------------
``llm.{provider}_api_key``   — secret, encrypted at rest
``llm.{provider}_base_url``  — plain string

Where ``provider`` is one of the three UI-visible slots:

* ``openai``    → fast-agent provider "openai"    (env ``OPENAI_API_KEY``)
* ``anthropic`` → fast-agent provider "anthropic" (env ``ANTHROPIC_API_KEY``)
* ``generic``   → fast-agent provider "generic"   (env ``GENERIC_API_KEY``)
  "Custom API" in the UI maps to ``generic`` because fast-agent's generic
  provider is the canonical slot for OpenAI-compatible proxies (CLIProxyAPI,
  9router, local Ollama, …) — using it avoids clobbering a user's real OpenAI
  credentials when they also run a local proxy.
"""
from __future__ import annotations

import errno
import logging
import os
import re
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# ---- Public contract -------------------------------------------------------

SUPPORTED_PROVIDERS: tuple[str, ...] = ("openai", "anthropic", "generic")

_LLM_SUBKEY_RE = re.compile(
    r"^(?P<provider>openai|anthropic|generic)_(?P<kind>api_key|base_url)$"
)

# Fast-agent's validate_provider_keys_post_creation iterates over every agent's
# configured provider and raises ProviderKeyError on empty/missing api_key.
# For providers the user hasn't set up yet, we keep a non-empty sentinel so
# startup doesn't crash — the wizard replaces it with the real key.
_API_KEY_PLACEHOLDER = "BOOTSTRAP_PLACEHOLDER_CONFIGURE_IN_WIZARD"

# YAML source of truth that fast-agent loads at startup of each agent process.
# We only touch the three top-level provider sections; everything else in the
# file (MCP server env, other providers' settings, etc.) is preserved as-is.
_SECRETS_YAML = Path(__file__).resolve().parent.parent / "fastagent.secrets.yaml"


# ---- DB → env + YAML --------------------------------------------------------


def _env_api_key_name(provider: str) -> str:
    """Mirror fast-agent's ProviderKeyManager convention."""
    return f"{provider.upper()}_API_KEY"


def _env_base_url_name(provider: str) -> str:
    """Unofficial but convenient — we also export base_url as an env var so
    anything that reads the environment directly (tests, custom scripts) sees
    the same value that fast-agent will read from the YAML.
    """
    return f"{provider.upper()}_BASE_URL"


def apply_llm_provider_change(
    provider: str,
    kind: str,
    new_value: Optional[str],
    *,
    action: str,
) -> None:
    """Reflect a single DB mutation into env vars and the secrets YAML.

    Safe to call repeatedly; both sinks are set to the final value (or cleared
    on delete).  Failures to write the YAML are logged but do not raise so a
    missing secrets file doesn't break the calling request — env is still updated.
    """
    provider = provider.lower()
    if provider not in SUPPORTED_PROVIDERS:
        return  # unknown provider — ignored, nothing to sync

    # --- env side ---
    if kind == "api_key":
        env_name = _env_api_key_name(provider)
    else:
        env_name = _env_base_url_name(provider)

    if action == "delete" or new_value in (None, ""):
        os.environ.pop(env_name, None)
        logger.info("[LLM_SYNC] Unset %s", env_name)
    else:
        os.environ[env_name] = str(new_value)
        logger.info("[LLM_SYNC] Set %s (%d chars)", env_name, len(str(new_value)))

    # --- YAML side ---
    try:
        _patch_secrets_yaml(provider, kind, new_value, action=action)
    except Exception:
        logger.exception("[LLM_SYNC] YAML patch failed for %s.%s", provider, kind)


def _patch_secrets_yaml(
    provider: str, kind: str, new_value: Optional[str], *, action: str
) -> None:
    """Update the `{provider}` section of fastagent.secrets.yaml in place.

    We use a plain ``yaml.safe_load`` + ``yaml.safe_dump`` round-trip.  This
    drops comments from the target file, which is an acceptable trade-off —
    the file is gitignored and the only structured content comes from either
    the wizard or this sync path.  Other top-level keys (``mcp``, ``google``,
    user's own additions) are preserved because we mutate the in-memory dict
    rather than rewriting from scratch.
    """
    data = {}
    if _SECRETS_YAML.exists():
        try:
            loaded = yaml.safe_load(_SECRETS_YAML.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except yaml.YAMLError as exc:
            logger.warning(
                "[LLM_SYNC] Existing %s is invalid YAML (%s) — refusing to overwrite",
                _SECRETS_YAML.name,
                exc,
            )
            return

    section = data.get(provider)
    if not isinstance(section, dict):
        section = {}

    if action == "delete" or new_value in (None, ""):
        if kind == "api_key":
            # Keep a non-empty placeholder so fast-agent's startup validation
            # doesn't crash on providers the user hasn't configured; we never
            # drop the section itself for the same reason.
            section[kind] = _API_KEY_PLACEHOLDER
        else:
            section.pop(kind, None)
    else:
        section[kind] = str(new_value)

    # Always retain the provider section — dropping it would cause
    # validate_provider_keys_post_creation to raise on the next fast.run().
    data[provider] = section

    _atomic_write_yaml(data)


def ensure_provider_sections() -> None:
    """Guarantee openai/anthropic/generic sections exist with a non-empty api_key.

    Idempotent: reads the YAML, fills any missing section or empty api_key with
    :data:`_API_KEY_PLACEHOLDER`, and writes back only if something changed.
    Call at startup before fast-agent boots to survive past deploys that dropped
    empty sections, or hand-edits that left api_key blank.
    """
    if not _SECRETS_YAML.exists():
        return
    try:
        loaded = yaml.safe_load(_SECRETS_YAML.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        logger.warning("[LLM_SYNC] ensure_provider_sections skipped — invalid YAML: %s", exc)
        return
    data = loaded if isinstance(loaded, dict) else {}

    changed = False
    for provider in SUPPORTED_PROVIDERS:
        section = data.get(provider)
        if not isinstance(section, dict):
            section = {}
            changed = True
        api_key = section.get("api_key")
        if not isinstance(api_key, str) or not api_key:
            section["api_key"] = _API_KEY_PLACEHOLDER
            changed = True
        data[provider] = section

    if changed:
        try:
            _atomic_write_yaml(data)
            logger.info("[LLM_SYNC] ensure_provider_sections: backfilled missing placeholders")
        except Exception:
            logger.exception("[LLM_SYNC] ensure_provider_sections write failed")


def _atomic_write_yaml(data: dict) -> None:
    """Write via tmp → rename so a crash mid-write can't corrupt the file."""
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    tmp = _SECRETS_YAML.with_suffix(_SECRETS_YAML.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, _SECRETS_YAML)
    except OSError as exc:
        # Docker bind-mounts a single file by inode — rename-over-mount
        # fails with EBUSY on Linux. Fall back to truncate + in-place write.
        if exc.errno != errno.EBUSY:
            raise
        _SECRETS_YAML.write_text(text, encoding="utf-8")
        try: tmp.unlink()
        except OSError: pass
    logger.info("[LLM_SYNC] %s updated (%d bytes)", _SECRETS_YAML.name, len(text))


# ---- Reconcile at startup ---------------------------------------------------


def reconcile_from_db(config_service) -> None:
    """Push every stored per-provider value into env + YAML on startup.

    This is the idempotent counterpart to :func:`apply_llm_provider_change` —
    call it once at app boot so a fresh process picks up whatever the DB
    currently has, in case env/YAML drifted between restarts.
    """
    for provider in SUPPORTED_PROVIDERS:
        for kind in ("api_key", "base_url"):
            value = config_service.get("llm", f"{provider}_{kind}")
            if value:
                apply_llm_provider_change(
                    provider, kind, value, action="update"
                )


# ---- Listener bridge --------------------------------------------------------


def parse_llm_key(key: str) -> Optional[tuple[str, str]]:
    """Parse ``{provider}_{kind}`` → (provider, kind) or return None.

    Used by the runtime_config dispatcher to recognise per-provider changes
    without the caller needing to know the regex.
    """
    m = _LLM_SUBKEY_RE.match(key)
    if not m:
        return None
    return m.group("provider"), m.group("kind")


# ---- Legacy migration -------------------------------------------------------


def migrate_legacy_keys(config_service) -> None:
    """Move pre-D2 ``llm.api_key``/``llm.base_url`` into the per-provider slots.

    The old schema stored exactly one (api_key, base_url) pair regardless of
    which provider was active.  New installs won't hit this branch; existing
    users running a mid-refactor build will get their current config migrated
    to the namespace of whichever ``llm.provider`` was active.

    Idempotent: once the legacy keys are removed the function is a no-op.
    """
    legacy_key = config_service.get("llm", "api_key")
    legacy_base = config_service.get("llm", "base_url")
    if legacy_key is None and legacy_base is None:
        return

    active = (config_service.get("llm", "provider") or "anthropic").lower()
    # UI's "custom" historically mapped to the same slot fast-agent calls
    # "generic" — translate at migration time so the stored namespace matches
    # the new convention.
    if active == "custom":
        active = "generic"
    if active not in SUPPORTED_PROVIDERS:
        logger.warning(
            "[LLM_SYNC] Legacy provider %r not in supported set; defaulting to anthropic",
            active,
        )
        active = "anthropic"

    items: list[tuple[str, str, Optional[str], bool]] = []
    if legacy_key:
        items.append(("llm", f"{active}_api_key", legacy_key, True))
    if legacy_base:
        items.append(("llm", f"{active}_base_url", legacy_base, False))
    # Always clear the old keys so the migration is one-shot.
    items.append(("llm", "api_key", None, True))
    items.append(("llm", "base_url", None, False))

    config_service.set_many(items, source="migration")
    logger.info(
        "[LLM_SYNC] Migrated legacy llm.api_key/llm.base_url into llm.%s_*",
        active,
    )
