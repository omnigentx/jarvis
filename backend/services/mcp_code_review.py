"""Content-bound human review before generated MCP code is executed or shared."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from services.approval_gate import request_approval

MAX_REVIEW_BYTES = 256 * 1024
SOURCE_SUFFIXES = {".py", ".txt", ".toml", ".json", ".yaml", ".yml", ".sh"}
IGNORED_DIRS = {".venv", ".git", "__pycache__", "logs", ".pytest_cache"}


def candidate_snapshot(directory: Path) -> tuple[str, str]:
    """Hash and render every candidate source file; reject hidden executable files."""
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("Generated MCP directory is missing or is a symlink")
    digest = hashlib.sha256()
    sections: list[str] = []
    total = 0
    seen_server = False
    for base, dirs, files in os.walk(directory, followlinks=False):
        if any((Path(base) / dirname).is_symlink() for dirname in dirs):
            raise ValueError("Generated MCP source contains a symlink")
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
        base_path = Path(base)
        for filename in sorted(files):
            path = base_path / filename
            relative = path.relative_to(directory).as_posix()
            if path.is_symlink() or not path.is_file():
                raise ValueError("Generated MCP source contains a symlink or special file")
            if path.suffix not in SOURCE_SUFFIXES:
                raise ValueError(f"Unexpected generated MCP file: {relative}")
            body = path.read_bytes()
            if relative == "manifest.json":
                manifest = json.loads(body)
                for transient in ("history", "updated_at", "last_stage",
                                  "last_stage_ok", "status", "promoted_at"):
                    manifest.pop(transient, None)
                body = json.dumps(manifest, sort_keys=True).encode("utf-8")
            total += len(body)
            if total > MAX_REVIEW_BYTES:
                raise ValueError("Generated MCP source exceeds review size limit")
            try:
                decoded = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"Generated MCP file is not UTF-8: {relative}") from exc
            digest.update(relative.encode("utf-8") + b"\0" + body + b"\0")
            sections.append(f"### {relative}\n\n````text\n{decoded}\n````")
            seen_server |= relative == "server.py"
    if not seen_server:
        raise ValueError("Generated MCP server.py is missing")
    return digest.hexdigest(), "\n\n".join(sections)


def freeze_candidate(directory: Path, frozen_root: Path, name: str) -> Path:
    """Copy reviewed source to a content-addressed runtime directory."""
    fingerprint, _ = candidate_snapshot(directory)
    target = frozen_root / name / fingerprint
    if target.exists():
        if candidate_snapshot(target)[0] != fingerprint:
            raise ValueError("Frozen MCP source was modified")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="reviewed-", dir=target.parent))
    try:
        for base, dirs, files in os.walk(directory, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
            relative_dir = Path(base).relative_to(directory)
            destination = temporary / relative_dir
            destination.mkdir(parents=True, exist_ok=True)
            for filename in files:
                shutil.copy2(Path(base) / filename, destination / filename,
                             follow_symlinks=False)
        if candidate_snapshot(temporary)[0] != fingerprint:
            raise ValueError("MCP source changed while it was being frozen")
        for path in temporary.rglob("*"):
            path.chmod(0o555 if path.is_dir() else 0o444)
        temporary.rename(target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return target


async def approve_candidate(
    name: str, directory: Path, *, action: str,
) -> tuple[bool, str]:
    """Require a human decision for exact source bytes, then recheck them."""
    fingerprint, sources = candidate_snapshot(directory)
    approved, reason = request_approval(
        approval_type=f"mcp_{action}",
        scope_key=f"mcp:{name}",
        title=f"Review MCP {action}: {name}",
        content_md=(
            f"Generated MCP server `{name}` requests **{action}**.\n\n"
            f"Source SHA-256: `{fingerprint}`\n\n"
            "Review all code, dependencies, network and file access before approving."
            f"\n\n{sources}"
        ),
    )
    if not approved:
        return False, reason
    current, _ = candidate_snapshot(directory)
    if current != fingerprint:
        return False, "source changed while approval was pending"
    return True, reason
