"""Pinned GitHub downloads into private runtime staging; never run installers."""
from __future__ import annotations

import io
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

from services.plugins.package import MAX_BYTES, MAX_FILES, PackageError

REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
COMMIT = re.compile(r"^[a-f0-9]{40}$")


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value:
        raise PackageError("Invalid package path")
    return path


def validate_source(repo: str, commit: str, subdirectory: str, approved: set[str]) -> None:
    """Allow only server-approved GitHub identities and immutable commits."""
    if not REPOSITORY.fullmatch(repo):
        raise PackageError("Invalid repository identity")
    if repo not in approved:
        raise PackageError("Repository is not approved")
    if not COMMIT.fullmatch(commit):
        raise PackageError("An immutable full commit SHA is required")
    _relative(subdirectory)


def extract_package(
    data: bytes, destination: Path, subdirectory: str, *, max_bytes: int = MAX_BYTES,
) -> Path:
    """Validate all archive entries before any writes, then extract one subtree."""
    relative = _relative(subdirectory)
    if len(data) > max_bytes:
        raise PackageError("Archive size limit exceeded")
    if destination.exists():
        raise PackageError("Destination must be a fresh private directory")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PackageError("Invalid ZIP archive") from exc
    with archive:
        seen: set[str] = set()
        top: set[str] = set()
        total = 0
        entries = archive.infolist()
        if len(entries) > MAX_FILES:
            raise PackageError("Archive size limit exceeded")
        for entry in entries:
            path = _relative(entry.filename)
            if not path.parts:
                raise PackageError("Invalid archive path")
            if entry.filename in seen:
                raise PackageError("Archive contains a duplicate member")
            seen.add(entry.filename)
            top.add(path.parts[0])
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise PackageError("Archive contains a symlink")
            if stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise PackageError("Archive contains a special file")
            total += entry.file_size
            if total > max_bytes:
                raise PackageError("Archive expanded size limit exceeded")
        if len(top) != 1:
            raise PackageError("Archive must have one root directory")
        prefix = PurePosixPath(next(iter(top))) / relative
        selected: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if path.is_relative_to(prefix) and path != prefix:
                selected.append((entry, path.relative_to(prefix)))
        if not selected:
            raise PackageError("Plugin subtree missing from archive")
        destination.mkdir(mode=0o700, parents=True)
        try:
            for entry, path in selected:
                target = destination / str(path)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Exclusive creation prevents aliases/duplicates overwriting.
                    with archive.open(entry) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, length=64 * 1024)
                    target.chmod(0o600)
        except Exception:
            shutil.rmtree(destination)
            raise
    return destination


async def download_package(
    repo: str, commit: str, subdirectory: str, destination: Path, approved: set[str],
) -> Path:
    """Fetch from one fixed HTTPS host, without redirects or inherited auth."""
    validate_source(repo, commit, subdirectory, approved)
    from services.plugins.fetch import fetch_package

    return await fetch_package(repo, commit, subdirectory, destination)
