"""Boot-time pre-flight checks.

Run early enough that a misconfigured environment fails with a clear
error instead of surfacing as a confusing crash deep in an unrelated
bootstrap step (Setup Wizard's Services step, git_credential_sync, etc.).

Lives in ``core/`` rather than ``server.py`` so tests can exercise the
checks without dragging ``agent.py`` (and the ``fast_agent`` submodule)
into the import graph.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def check_master_key_or_exit() -> None:
    """Fail fast at boot if ``JARVIS_MASTER_KEY`` is missing.

    Required for ANY backend boot — not conditional on whether the DB
    currently has encrypted rows. Reason: without the key, the very next
    user action that writes a secret (Setup Wizard saving an API key,
    OAuth callback persisting a token, llm_provider_sync receiving a
    new credential) raises ``MissingMasterKeyError`` deep inside a
    request handler and surfaces to the UI as a generic 500. Fail-loud
    here keeps the diagnostic at the layer that actually owns the
    invariant — operators see the env var name in the boot log instead
    of grepping a stack trace from a settings save.

    Earlier behavior (allow boot when DB has zero encrypted rows) was
    rejected for exactly this reason: it converted a config bug into a
    delayed mystery 500.
    """
    if os.environ.get("JARVIS_MASTER_KEY"):
        return
    logger.error(
        "[BOOTSTRAP] JARVIS_MASTER_KEY is not set. The backend cannot "
        "encrypt or decrypt secrets without it, so refusing to boot. "
        "Set JARVIS_MASTER_KEY in your environment (.env / docker-compose) "
        "and restart. Generate a new key with: "
        "python -c 'import secrets; print(secrets.token_urlsafe(32))'. "
        "If upgrading from a build that used JARVIS_API_KEY as the master, "
        "set JARVIS_MASTER_KEY to the same value as your current "
        "JARVIS_API_KEY once."
    )
    raise SystemExit(1)
