"""GitHub CLI (gh) credential sync — DB → in-container ``hosts.yml`` consumed
by the ``gh`` CLI when agents invoke it through the ``execute`` shell tool.

This is a sibling of :mod:`services.git_credential_sync`. Both mirror the same
DB row (``service.github.personal_access_token``) into different surfaces:

* ``git_credential_sync`` writes ``git-credentials`` + ``gitconfig`` so the
  ``git`` CLI can authenticate.
* This module writes ``$GH_CONFIG_DIR/hosts.yml`` (mode 0600) so ``gh`` CLI
  can authenticate without any ``GH_TOKEN`` env var. We deliberately avoid
  the env-var route because env values are visible to ``env`` / ``printenv``
  / ``ps eww`` inside the agent's shell, which would let a prompt-injected
  agent exfiltrate the token. The file lives outside ``~/.config/gh/`` so we
  never overwrite the developer's personal ``gh auth login`` state.

Auth model: ``gh`` reads ``$GH_CONFIG_DIR/hosts.yml`` (if ``GH_CONFIG_DIR``
is set) or ``~/.config/gh/hosts.yml`` (default). We set ``GH_CONFIG_DIR``
to a Jarvis-private directory at backend boot via ``os.environ`` so every
team-agent subprocess inherits it, and ``gh`` finds *our* hosts.yml rather
than the dev user's personal one.

DB key convention (reused from git_credential_sync, NOT duplicated):
    ``service.github.personal_access_token``  — secret, encrypted at rest
    ``service.github.user_name``              — plain (becomes ``user:`` field)
"""
from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
from typing import Optional

import yaml

from services.secret_utils import safe_get_or_none

logger = logging.getLogger(__name__)


_PERSIST_DIR = Path(os.getenv("JARVIS_PERSIST_DIR") or Path(__file__).resolve().parent.parent)
_GH_CONFIG_DIR = _PERSIST_DIR / ".gh-config"
_GH_HOSTS_PATH = _GH_CONFIG_DIR / "hosts.yml"

# Field keys under ``service.github`` — reused from git_credential_sync.
# Kept local so this module doesn't depend on it (and vice versa).
FIELD_TOKEN = "personal_access_token"
FIELD_USER_NAME = "user_name"
_KNOWN_FIELDS = (FIELD_TOKEN, FIELD_USER_NAME)


def _warn_skipped(exc: Exception) -> None:
    logger.warning(
        "[GH_SYNC] Skipped stale field: %s. "
        "Re-set via Settings → Services to restore.", exc,
    )


def apply_change(key: str, new_value: Optional[str], *, action: str) -> None:
    """Hot-reload hook called by ``runtime_config._on_config_change``.

    Fields outside ``_KNOWN_FIELDS`` are ignored — this module only cares
    about token + user_name. Other ``service.github.*`` fields (eg email)
    belong to :mod:`services.git_credential_sync` and are wired there.
    """
    if key not in _KNOWN_FIELDS:
        return
    from services.config_service import config_service as _cfg
    _write_from_values(
        token=safe_get_or_none(_cfg, "service.github", FIELD_TOKEN, on_warn=_warn_skipped),
        user_name=safe_get_or_none(_cfg, "service.github", FIELD_USER_NAME, on_warn=_warn_skipped),
    )


def reconcile_from_db(config_service) -> None:
    """Push current DB state into ``hosts.yml`` at startup and export
    ``GH_CONFIG_DIR`` so the gh CLI (and child agent subprocesses) read it.

    Idempotent. Safe to call when token is missing — the file is created
    empty/with no ``oauth_token`` field, which leaves ``gh`` unauth'd
    (the correct signal for a fresh install).
    """
    # Export GH_CONFIG_DIR for every gh invocation, including those spawned
    # by team-agent subprocesses via the fast-agent ``execute`` shell tool.
    # Set even if token is missing so behaviour is consistent — gh will say
    # "not authenticated" loudly rather than silently fall back to the dev
    # user's personal ~/.config/gh/.
    os.environ["GH_CONFIG_DIR"] = str(_GH_CONFIG_DIR)

    _write_from_values(
        token=safe_get_or_none(config_service, "service.github", FIELD_TOKEN, on_warn=_warn_skipped),
        user_name=safe_get_or_none(config_service, "service.github", FIELD_USER_NAME, on_warn=_warn_skipped),
    )


def _write_from_values(*, token: Optional[str], user_name: Optional[str]) -> None:
    """Atomically write ``hosts.yml`` from supplied field values.

    Raises on filesystem errors so a half-written auth state can't trip up
    agents later — same rationale as ``git_credential_sync._write_from_values``.
    """
    _GH_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Tighten dir perms so even read access to the directory is owner-only.
    try:
        os.chmod(_GH_CONFIG_DIR, 0o700)
    except OSError:
        # Non-fatal: some filesystems (e.g. macOS APFS with custom ACLs)
        # may refuse chmod but the file mode itself still enforces 0600.
        pass

    _atomic_write(_GH_HOSTS_PATH, _render_hosts(token, user_name), mode=0o600)


def _render_hosts(token: Optional[str], user_name: Optional[str]) -> str:
    """Produce the ``hosts.yml`` body that gh's stored-token auth expects.

    Schema (verified against gh 2.68.1):
        github.com:
            user: <username>
            oauth_token: <PAT>
            git_protocol: https

    When token is missing we write an empty top-level so gh reads it as
    "no hosts configured" — agents calling ``gh`` get a clear "not logged
    in" error rather than failing on a malformed file.
    """
    if not token:
        return ""
    host_entry: dict[str, str] = {"git_protocol": "https"}
    if user_name:
        host_entry["user"] = user_name
    host_entry["oauth_token"] = token
    return yaml.safe_dump({"github.com": host_entry}, sort_keys=False)


def _atomic_write(path: Path, text: str, *, mode: int) -> None:
    """Mirror of :func:`services.git_credential_sync._atomic_write` — same
    tmp → rename dance, same Docker-bind-mount EBUSY fallback, same
    fail-loud philosophy. Duplicated rather than imported to keep the two
    sync modules independent (one can fail without taking down the other).
    """
    data = text.encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
        try:
            os.replace(tmp, path)
        except OSError as exc:
            if exc.errno != errno.EBUSY:
                raise
            path.write_bytes(data)
            os.chmod(path, mode)
            try:
                tmp.unlink()
            except OSError:
                pass
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    logger.info("[GH_SYNC] %s updated (%d bytes, mode %o)", path.name, len(data), mode)
