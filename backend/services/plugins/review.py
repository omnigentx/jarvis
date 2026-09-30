"""Human source review: immutable, bounded, text-only; never execute/render."""

from __future__ import annotations

from pathlib import PurePosixPath

from services.plugins.lifecycle import PluginStateError
from services.plugins.package import inspect_package

MAX_REVIEW_BYTES = 256 * 1024


def review_package(lifecycle, identity: str, relative: str | None = None) -> dict:
    record = lifecycle.get(identity)
    root = lifecycle.package_path(identity)
    package = inspect_package(root)
    if package.digest != record["digest"]:
        raise PluginStateError("Package changed after inspection")
    if relative is None:
        return {
            "digest": package.digest,
            "files": [
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": path.stat().st_size,
                }
                for path in sorted(root.rglob("*"))
                if path.is_file()
            ],
        }
    candidate = PurePosixPath(relative)
    if (
        not relative
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "\\" in relative
    ):
        raise PluginStateError("Invalid package review path")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise PluginStateError("Package review file not found")
    if path.stat().st_size > MAX_REVIEW_BYTES:
        raise PluginStateError(
            "Review file exceeds 256 KiB; inspect the pinned upstream source"
        )
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise PluginStateError("Binary file cannot be rendered as source text") from exc
    return {"digest": package.digest, "path": relative, "content": content}
