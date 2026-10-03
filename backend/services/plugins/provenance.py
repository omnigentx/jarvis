"""Server-owned reputation labels; these never grant execution permission."""

from __future__ import annotations

OFFICIAL_REPOSITORIES = {
    "openai/plugins": "OpenAI",
    "anthropics/claude-code": "Anthropic",
}


def source_origin(repo: str, *, marketplace_repo: str | None = None) -> dict:
    """Classify exact GitHub identity; catalog inclusion is not endorsement."""
    publisher = OFFICIAL_REPOSITORIES.get(repo.casefold())
    marketplace = OFFICIAL_REPOSITORIES.get((marketplace_repo or "").casefold())
    return {
        "kind": "official" if publisher else "community" if marketplace else "external",
        "publisher": publisher,
        "marketplace": marketplace,
        "repo": repo,
    }
