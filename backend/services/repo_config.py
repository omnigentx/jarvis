"""Repository URL resolution for agent git operations.

Agents that clone, push, or reference the project repo call
:func:`get_repo_url` instead of hardcoding URLs.

Resolution: read ``service.jarvis_repo / JARVIS_REPO_URL`` from
:class:`~services.config_service.ConfigService`. The Setup Wizard and
Settings → Services UI are the only writers — DB is the single source of
truth. If the value is unset we raise :class:`RuntimeError` so callers
fail loud instead of pushing to whatever ``git remote origin`` happens to
point at.
"""

from __future__ import annotations

from services.config_service import config_service

CATEGORY = "service.jarvis_repo"
KEY = "JARVIS_REPO_URL"


def get_repo_url() -> str:
    value = config_service.get(CATEGORY, KEY)
    if value is None or not value.strip():
        raise RuntimeError(
            f"{CATEGORY}/{KEY} is not configured. "
            "Run the setup wizard or set it in Settings → Services."
        )
    return value.strip()
