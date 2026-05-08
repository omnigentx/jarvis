"""Bootstrap-safe wrappers for reading encrypted secrets.

The single function in this module — :func:`safe_get_or_none` — exists to
make backend boot resilient to one specific failure mode: a secret stored
in :class:`~services.config_service.ConfigService` was encrypted under
master key A, the master key has since rotated to B, and the row was not
re-encrypted. ``ConfigService.get`` is fail-closed for this case (raises
:class:`~core.secrets_crypto.DecryptError`) so that *runtime* callers
don't silently consume a wrong value. At *bootstrap* the same exception
would crash the container before the user can reach the Settings UI to
re-set the value.

Bootstrap modules calling this — :func:`services.runtime_config.
reconcile_service_env`, :func:`services.git_credential_sync.
reconcile_from_db`, :func:`services.llm_provider_sync.reconcile_from_db`
and :func:`services.llm_provider_sync.migrate_legacy_keys`. Each one
reads one or more *secret* fields, and a single stale value would
otherwise take the whole backend offline. Soft-fail per field instead.

Why a dedicated module
----------------------

Originally lived in ``runtime_config`` but pulling that import into
``llm_provider_sync`` (which ``runtime_config`` already imports) caused a
circular import. Splitting the helper out of any module that participates
in that chain keeps both ends free of cycle workarounds.

Scope
-----

Strictly :class:`~core.secrets_crypto.DecryptError` is swallowed. Every
other ``RuntimeError`` (DB connection drop, missing table, …) and every
other exception class (programming bugs, IO errors) propagates.
Broadening this would hide infrastructure problems behind a silent
``None`` and reproduce the exact "swallow at boot, surface nowhere"
anti-pattern the original :class:`ConfigService` design avoids.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from core.secrets_crypto import DecryptError

logger = logging.getLogger(__name__)


def safe_get_or_none(
    service: Any,
    category: str,
    key: str,
    *,
    on_warn: Optional[Callable[[Exception], None]] = None,
) -> Optional[str]:
    """Read a secret from ``service`` in a bootstrap-safe way.

    Returns the stored value on a clean read, ``None`` when the row is
    absent, and ``None`` when the row is present but its ciphertext is
    stale (master key rotated without re-encrypt). In the stale case
    ``on_warn`` — when provided — receives the original exception so the
    caller can log a warning in its own module's namespace.

    Any other exception (e.g. ``RuntimeError("database is locked")``,
    programming bugs) propagates. Broadening the catch would hide real
    defects.

    Args:
        service: Anything with a ``get(category, key)`` method —
            normally :class:`ConfigService`. Typed loosely so unit tests
            can pass a stub without dragging the real DB in.
        category: Config category (e.g. ``"service.github"``).
        key: Field name (e.g. ``"personal_access_token"``).
        on_warn: Optional callback invoked exactly once per stale-row
            read with the upstream :class:`DecryptError`. Caller-supplied
            so the warning shows up in the caller's logger / module
            namespace ("[GIT_SYNC]", "[LLM_SYNC]", …).

    Returns:
        The decrypted value, or ``None`` if absent / undecryptable.
    """
    try:
        return service.get(category, key)
    except DecryptError as exc:
        if on_warn is not None:
            on_warn(exc)
        return None
