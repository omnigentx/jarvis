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
    """Fail fast if ``JARVIS_MASTER_KEY`` is missing while the DB has
    encrypted secrets.

    Without this, the missing key surfaces inside an unrelated bootstrap
    step (Wizard's Services step calling ``ensure_provider_sections``,
    or ``git_credential_sync.reconcile_from_db``) with a confusing stack
    trace pointing at the wrong layer.

    Fresh installs (no encrypted rows yet) are allowed to boot — the key
    only becomes mandatory when there's something to decrypt.
    """
    if os.environ.get("JARVIS_MASTER_KEY"):
        return
    from core.database import SessionLocal, SystemConfig
    with SessionLocal() as db:
        has_secrets = (
            db.query(SystemConfig).filter(SystemConfig.is_secret == True).first()  # noqa: E712
            is not None
        )
    if has_secrets:
        logger.error(
            "[BOOTSTRAP] JARVIS_MASTER_KEY is not set but the DB contains "
            "encrypted secrets. Set JARVIS_MASTER_KEY in your environment "
            "(.env / docker-compose) and restart. Generate a new key with: "
            "python -c 'import secrets; print(secrets.token_urlsafe(32))'. "
            "If upgrading from a build that used JARVIS_API_KEY as the master, "
            "set JARVIS_MASTER_KEY to the same value as your current "
            "JARVIS_API_KEY once."
        )
        raise SystemExit(1)
