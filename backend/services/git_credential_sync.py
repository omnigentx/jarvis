"""Git credential sync — DB → in-container files consumed by git CLI + github MCP.

Problem this solves: dev agents run ``git clone/pull/push`` through the
``execute`` shell tool inside ``jarvis_backend``. The container has no
GitHub credential on its own — the ``GITHUB_PERSONAL_ACCESS_TOKEN`` in
``fastagent.secrets.yaml`` is only consumed by the ``github`` MCP
subprocess, not exported to the container env. Exposing it via env would
leak into ``env``/``ps``/crash dumps; we want a narrower surface.

Solution: mirror the user's GitHub credential from DB rows under
``service.github.*`` into three sinks:

* ``git-credentials`` (mode 0600) — one line ``https://x-access-token:<token>@github.com``
* ``gitconfig`` (mode 0644) — INI with ``credential.helper = store --file=...``
  and optional ``[user]`` identity block
* ``fastagent.secrets.yaml`` → ``mcp.servers.github.env.GITHUB_PERSONAL_ACCESS_TOKEN``
  (so the ``github`` MCP server uses the same rotated value — keeps docker
  and local flows consistent; docker-compose mounts
  ``fastagent.secrets.docker.yaml`` at the same container path)

The two git files live in the container workspace (``/app/git-credentials``
and ``/app/gitconfig``) and are regenerated from the DB on every backend
boot via :func:`reconcile_from_db`. The token itself is persisted in the
``jarvis-data`` named volume that holds ``data/jarvis.db``, so rotation
across deploys goes: wizard saves → DB encrypts → next boot re-materializes
the files. ``docker-compose.yaml`` sets ``GIT_CONFIG_GLOBAL=/app/gitconfig``
so every git invocation inside the container transparently picks up the
helper — agents run plain ``git clone https://github.com/owner/repo`` with
no credential in args, env, or ps output.

DB key convention
-----------------
``service.github.personal_access_token``  — secret, encrypted at rest
``service.github.user_name``              — plain
``service.github.user_email``             — plain
"""
from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from services.secret_utils import safe_get_or_none

logger = logging.getLogger(__name__)


_PERSIST_DIR = Path(os.getenv("JARVIS_PERSIST_DIR") or Path(__file__).resolve().parent.parent)
_GIT_CREDENTIALS_PATH = _PERSIST_DIR / "git-credentials"
_GITCONFIG_PATH = _PERSIST_DIR / "gitconfig"

# Container-side absolute path written into the generated gitconfig's
# ``credential.helper`` so git finds the credential regardless of ``$HOME``.
# Must match :data:`_GIT_CREDENTIALS_PATH` as observed from inside the
# container — same file, absolute path spelling.
#
# NOTE: neither this path nor :data:`_GITCONFIG_PATH` follows git's
# standard auto-discovery rules (``~/.gitconfig`` / ``~/.git-credentials``).
# docker-compose MUST set ``GIT_CONFIG_GLOBAL=/app/gitconfig`` in the
# container env — unsetting it will leave git looking in ``$HOME`` and the
# helper chain will silently break.
_CONTAINER_CREDENTIALS_PATH = "/app/git-credentials"

# Field keys under ``service.github``. Kept as constants so both this module
# and tests reference the same strings.
FIELD_TOKEN = "personal_access_token"
FIELD_USER_NAME = "user_name"
FIELD_USER_EMAIL = "user_email"
_KNOWN_FIELDS = (FIELD_TOKEN, FIELD_USER_NAME, FIELD_USER_EMAIL)


# ---- DB → files ------------------------------------------------------------


def _warn_skipped(exc: Exception) -> None:
    """Per-field decrypt-fail warning. Surfaced once per stale secret so ops
    can see exactly which row needs re-setting in the wizard."""
    logger.warning(
        "[GIT_SYNC] Skipped stale field: %s. "
        "Re-set via Settings → Services to restore.", exc,
    )


def apply_change(key: str, new_value: Optional[str], *, action: str) -> None:
    """Reflect a single ``service.github.<key>`` mutation into the two files.

    Called by ``runtime_config._on_config_change`` on every config write. We
    re-read *all three* fields from the DB on each call rather than
    reconstructing partial state, because the two output files are whole-file
    overwrites — doing it field-by-field would risk writing a half-updated
    gitconfig if two changes land interleaved.

    ``new_value`` and ``action`` are accepted for signature parity with the
    other ``apply_*`` functions (``apply_llm_provider_change`` etc.) but are
    intentionally unused — the DB re-read is authoritative.

    Tolerant of decrypt-fail: a stale field encrypted under a rotated master
    key is treated as missing rather than crashing the hot-reload path. Same
    rationale as :func:`reconcile_from_db`.
    """
    if key not in _KNOWN_FIELDS:
        return  # Unknown field under service.github — ignore, stay forward-compat

    # Defer to the reconcile path which always reads the *full* current state
    # from the DB. This makes apply_change idempotent and race-tolerant.
    from services.config_service import config_service as _cfg
    _write_from_values(
        token=safe_get_or_none(_cfg, "service.github", FIELD_TOKEN, on_warn=_warn_skipped),
        user_name=safe_get_or_none(_cfg, "service.github", FIELD_USER_NAME, on_warn=_warn_skipped),
        user_email=safe_get_or_none(_cfg, "service.github", FIELD_USER_EMAIL, on_warn=_warn_skipped),
    )


def reconcile_from_db(config_service) -> None:
    """Push current DB state into both files at startup.

    Idempotent counterpart to :func:`apply_change`. Ensures the host files
    exist (empty is fine) so the Docker bind-mount doesn't dangle on a
    fresh install where the user hasn't run the wizard yet.

    Decrypt-fail tolerance: a stale ``personal_access_token`` encrypted
    under a rotated master key is treated as missing — git-credentials gets
    written empty (git prompts for credential, the correct "no credential"
    signal). Filesystem errors still propagate so disk-full / permission
    problems crash startup loud, matching the intent at server.py:265-278.
    """
    _write_from_values(
        token=safe_get_or_none(config_service, "service.github", FIELD_TOKEN, on_warn=_warn_skipped),
        user_name=safe_get_or_none(config_service, "service.github", FIELD_USER_NAME, on_warn=_warn_skipped),
        user_email=safe_get_or_none(config_service, "service.github", FIELD_USER_EMAIL, on_warn=_warn_skipped),
    )


# ---- File writers ----------------------------------------------------------


def _write_from_values(
    *,
    token: Optional[str],
    user_name: Optional[str],
    user_email: Optional[str],
) -> None:
    """Atomically write all three sinks from the supplied field values.

    Raises on filesystem errors for the two git files (``git-credentials`` +
    ``gitconfig``) — they are the contract with the agent shell, so failure
    must surface immediately rather than leaving a half-written state that
    only manifests at git-invocation time. The github MCP yaml patch stays
    best-effort: if the secrets yaml is missing or malformed the git CLI
    side still works, and the next MCP tool call will surface the problem.
    """
    # mkdir errors are fatal — without the persist dir nothing else can work.
    _PERSIST_DIR.mkdir(parents=True, exist_ok=True)

    _atomic_write(_GIT_CREDENTIALS_PATH, _render_credentials(token), mode=0o600)
    _atomic_write(_GITCONFIG_PATH, _render_gitconfig(user_name, user_email), mode=0o644)
    # Keep the github MCP token in lockstep with the git CLI token so rotation
    # through the wizard updates both surfaces in one shot.
    try:
        _patch_secrets_yaml_github_token(token)
    except Exception:
        logger.exception("[GIT_SYNC] MCP secrets yaml patch failed")


def _render_credentials(token: Optional[str]) -> str:
    """Produce the ``~/.git-credentials`` body git's ``store`` helper expects.

    ``quote`` escapes any reserved URL chars (``@ : / # ? % + &``) so
    future fine-grained PATs and non-classic token formats don't produce a
    malformed URL that git silently rejects. Classic ``ghp_…`` tokens only
    use ``[A-Za-z0-9_]`` so pass through unchanged.
    """
    if not token:
        # Keep the file present (bind-mount needs a valid inode) but empty —
        # git will prompt for a username on HTTPS clone, which is the correct
        # "no credential configured" signal.
        return ""
    return f"https://x-access-token:{quote(token, safe='')}@github.com\n"


def _render_gitconfig(user_name: Optional[str], user_email: Optional[str]) -> str:
    """Produce the ``~/.gitconfig`` git reads on every invocation."""
    lines = [
        "[credential]",
        f"\thelper = store --file={_CONTAINER_CREDENTIALS_PATH}",
    ]
    if user_name or user_email:
        lines.append("[user]")
        if user_name:
            lines.append(f"\tname = {user_name}")
        if user_email:
            lines.append(f"\temail = {user_email}")
    lines.append("")  # trailing newline
    return "\n".join(lines)


def _patch_secrets_yaml_github_token(token: Optional[str]) -> None:
    """Update ``mcp.servers.github.env.GITHUB_PERSONAL_ACCESS_TOKEN`` in place.

    Target file is the same ``fastagent.secrets.yaml`` that
    :mod:`services.llm_provider_sync` owns — we reuse its path constant so the
    docker bind-mount indirection (``fastagent.secrets.docker.yaml`` on host
    mounted as ``fastagent.secrets.yaml`` in container) stays a single-knob
    abstraction. Other top-level keys (``openai``, ``anthropic``, other MCP
    servers) are preserved because we mutate the parsed dict rather than
    rewriting from scratch. Comments are lost — acceptable since the file is
    gitignored and only structured content written here is source-of-truth.
    """
    import yaml
    from services.llm_provider_sync import _SECRETS_YAML

    data: dict = {}
    if _SECRETS_YAML.exists():
        try:
            loaded = yaml.safe_load(_SECRETS_YAML.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except yaml.YAMLError as exc:
            logger.warning(
                "[GIT_SYNC] %s invalid YAML (%s) — skipping github MCP token sync",
                _SECRETS_YAML.name,
                exc,
            )
            return

    mcp_section = data.get("mcp")
    if not isinstance(mcp_section, dict):
        mcp_section = {}
        data["mcp"] = mcp_section
    servers = mcp_section.get("servers")
    if not isinstance(servers, dict):
        servers = {}
        mcp_section["servers"] = servers
    github = servers.get("github")
    if not isinstance(github, dict):
        github = {}
        servers["github"] = github
    env_section = github.get("env")
    if not isinstance(env_section, dict):
        env_section = {}
        github["env"] = env_section

    if token:
        env_section["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
    else:
        env_section.pop("GITHUB_PERSONAL_ACCESS_TOKEN", None)

    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    _atomic_write(_SECRETS_YAML, text, mode=0o600)


def _atomic_write(path: Path, text: str, *, mode: int) -> None:
    """Write via tmp → rename, with EBUSY fallback for Docker bind-mounts.

    Mirrors :func:`services.llm_provider_sync._atomic_write_yaml`. Never logs
    the payload — only the destination path and byte count. Raises on any
    filesystem error the caller should surface (read-only mount, ENOSPC,
    permission denied …) — we want the backend to fail fast at startup
    rather than leave git ops to crash later with opaque messages.
    """
    data = text.encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
        try:
            os.replace(tmp, path)
        except OSError as exc:
            # Docker bind-mounts a single file by inode — rename-over-mount
            # fails with EBUSY on Linux. Fall back to in-place truncate+write.
            if exc.errno != errno.EBUSY:
                raise
            path.write_bytes(data)
            os.chmod(path, mode)
            try:
                tmp.unlink()
            except OSError:
                pass
    except Exception:
        # Clean up the stray tmp file (best-effort) and re-raise so the
        # caller sees the real error — swallowing it would let startup
        # continue with a half-written credential file, which fails later
        # at git invocation time with a confusing error.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    logger.info("[GIT_SYNC] %s updated (%d bytes, mode %o)", path.name, len(data), mode)
