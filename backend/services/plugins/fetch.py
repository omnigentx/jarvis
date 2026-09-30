"""Download only an immutable plugin subtree from fixed GitHub hosts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx

from services.plugins.archive import COMMIT, REPOSITORY, _relative
from services.plugins.package import MAX_BYTES, MAX_FILES, PackageError


async def _read(client: httpx.AsyncClient, url: str, limit: int) -> bytes:
    data = bytearray()
    async with client.stream("GET", url, follow_redirects=False) as response:
        response.raise_for_status()
        async for block in response.aiter_bytes():
            data.extend(block)
            if len(data) > limit:
                raise PackageError("Plugin download size limit exceeded")
    return bytes(data)


async def _tree(client: httpx.AsyncClient, repo: str, sha: str, *, recursive: bool = False) -> list[dict]:
    url = f"https://api.github.com/repos/{repo}/git/trees/{sha}"
    data = json.loads(await _read(client, url + ("?recursive=1" if recursive else ""), 2 * 1024 * 1024))
    if not isinstance(data, dict) or data.get("truncated") or not isinstance(data.get("tree"), list):
        raise PackageError("Incomplete Git tree inventory")
    return data["tree"]


async def fetch_package(repo: str, commit: str, subdirectory: str, destination: Path,
                        *, client: httpx.AsyncClient | None = None) -> Path:
    """Validate all selected entries before writing, then verify Git blob hashes."""
    if not REPOSITORY.fullmatch(repo) or not COMMIT.fullmatch(commit):
        raise PackageError("Invalid pinned source")
    directory = _relative(subdirectory)
    if len(directory.parts) > 16:
        raise PackageError("Plugin directory depth limit exceeded")
    if client is None:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as owned:
            return await fetch_package(repo, commit, subdirectory, destination, client=owned)
    sha = commit
    for part in directory.parts:
        entries = await _tree(client, repo, sha)
        matches = [entry for entry in entries if entry.get("path") == part and entry.get("type") == "tree"]
        if len(matches) != 1 or not COMMIT.fullmatch(matches[0].get("sha", "")):
            raise PackageError("Plugin subtree not found")
        sha = matches[0]["sha"]
    entries = await _tree(client, repo, sha, recursive=True)
    if len(entries) > MAX_FILES:
        raise PackageError("Plugin file count limit exceeded")
    files = []
    total = 0
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise PackageError("Invalid Git tree entry")
        path = PurePosixPath(entry["path"])
        if path.is_absolute() or ".." in path.parts or "\\" in entry["path"] or path == PurePosixPath(".") or str(path) in seen:
            raise PackageError("Unsafe Git tree path")
        seen.add(str(path))
        if entry.get("type") == "tree" and entry.get("mode") == "040000":
            continue
        if entry.get("type") != "blob" or entry.get("mode") not in {"100644", "100755"}:
            raise PackageError("Symlinks and submodules are unsupported")
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or not COMMIT.fullmatch(entry.get("sha", "")):
            raise PackageError("Invalid Git blob metadata")
        total += size
        if total > MAX_BYTES:
            raise PackageError("Plugin expanded size limit exceeded")
        files.append((path, size, entry["sha"]))
    if not files:
        raise PackageError("Empty plugin subtree")
    destination.mkdir(parents=True, mode=0o700)
    try:
        for path, size, blob in files:
            relative = str(directory / path)
            url = f"https://raw.githubusercontent.com/{repo}/{commit}/{quote(relative, safe='/')}"
            data = await _read(client, url, size)
            digest = hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest()
            if len(data) != size or digest != blob:
                raise PackageError("Git blob integrity check failed")
            target = destination / str(path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            await asyncio.to_thread(target.write_bytes, data)
            target.chmod(0o600)
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, destination, True)
        raise
    return destination
