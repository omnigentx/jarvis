"""Normalize untrusted marketplace catalogs; external sources retain identity."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from services.plugins.archive import COMMIT, REPOSITORY, _relative
from services.plugins.package import NAME, PackageError
from services.plugins.provenance import source_origin

MAX_CATALOG_BYTES = 2 * 1024 * 1024


def normalize_marketplace(
    catalog: dict[str, Any], repo: str, commit: str
) -> list[dict[str, Any]]:
    """Map catalog entries without allowing their labels to confer trust."""
    plugins = catalog.get("plugins")
    if not isinstance(plugins, list) or len(plugins) > 1000:
        raise PackageError("Invalid marketplace plugins")
    items: list[dict[str, Any]] = []
    for plugin in plugins:
        if (
            not isinstance(plugin, dict)
            or not isinstance(plugin.get("name"), str)
            or not NAME.fullmatch(plugin["name"])
        ):
            raise PackageError("Invalid catalog plugin name")
        source = plugin.get("source")
        external = False
        source_repo, source_commit, directory = repo, commit, ""
        if isinstance(source, str):
            if not source.startswith("./"):
                raise PackageError("Invalid catalog source path")
            directory = str(_relative(source))
        elif isinstance(source, dict):
            kind = source.get("source")
            if kind in {"local", "directory"}:
                path = source.get("path")
                if not isinstance(path, str):
                    raise PackageError("Invalid catalog source path")
                directory = str(_relative(path))
            elif kind == "github":
                source_repo = source.get("repo", "")
                if not isinstance(source_repo, str) or not REPOSITORY.fullmatch(
                    source_repo
                ):
                    raise PackageError("Invalid catalog repository")
                external = source_repo != repo
                ref = source.get("sha", source.get("ref", ""))
                source_commit = (
                    ref if isinstance(ref, str) and COMMIT.fullmatch(ref) else None
                )
                directory = str(_relative(source.get("path", "")))
            elif kind in {"url", "git-subdir"}:
                url = source.get("url")
                if not isinstance(url, str):
                    raise PackageError("Invalid catalog repository URL")
                parsed = urlsplit(url)
                identity = parsed.path.removeprefix("/").removesuffix(".git")
                if (
                    parsed.scheme != "https"
                    or parsed.netloc != "github.com"
                    or parsed.query
                    or parsed.fragment
                    or not REPOSITORY.fullmatch(identity)
                ):
                    raise PackageError("Invalid catalog repository URL")
                source_repo = identity
                external = source_repo != repo
                ref = source.get("sha", source.get("ref", ""))
                source_commit = (
                    ref if isinstance(ref, str) and COMMIT.fullmatch(ref) else None
                )
                path = source.get("path", "") if kind == "git-subdir" else ""
                if not isinstance(path, str):
                    raise PackageError("Invalid catalog source path")
                directory = str(_relative(path))
            else:
                raise PackageError("Unsupported marketplace source type")
        else:
            raise PackageError("Missing marketplace source")
        items.append(
            {
                "name": plugin["name"],
                "description": str(plugin.get("description", ""))[:2000],
                "repo": source_repo,
                "commit": source_commit,
                "subdirectory": "" if directory == "." else directory,
                "requires_source_approval": external,
                "source_origin": source_origin(source_repo, marketplace_repo=repo),
            }
        )
    return items


async def _github_json(client: httpx.AsyncClient, path: str) -> Any:
    data = bytearray()
    async with client.stream(
        "GET",
        "https://api.github.com/" + path,
        headers={"Accept": "application/vnd.github.raw+json"},
    ) as response:
        response.raise_for_status()
        async for block in response.aiter_bytes():
            data.extend(block)
            if len(data) > MAX_CATALOG_BYTES:
                raise PackageError("Marketplace size limit exceeded")
    import json

    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise PackageError("Invalid marketplace JSON") from exc


async def discover(repo: str) -> dict[str, Any]:
    """Read metadata only from GitHub; user action triggers sync, never polling."""
    if not REPOSITORY.fullmatch(repo):
        raise PackageError("Invalid repository identity")
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=False, trust_env=False
    ) as client:
        head = await _github_json(client, f"repos/{repo}/commits/HEAD")
        commit = head.get("sha", "")
        if not COMMIT.fullmatch(commit):
            raise PackageError("GitHub did not resolve immutable commit")
        for path in (
            ".agents/plugins/marketplace.json",
            ".claude-plugin/marketplace.json",
        ):
            try:
                catalog = await _github_json(
                    client, f"repos/{repo}/contents/{path}?ref={commit}"
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    continue
                raise
            if not isinstance(catalog, dict):
                raise PackageError("Invalid marketplace object")
            return {
                "repo": repo,
                "commit": commit,
                "plugins": normalize_marketplace(catalog, repo, commit),
            }
    raise PackageError("Supported marketplace catalog not found")
