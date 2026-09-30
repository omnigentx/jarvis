"""Bounded disposable snapshots; active and uncertain bindings never evict."""

from __future__ import annotations

import fcntl
import shutil
import time
import uuid
from pathlib import Path

MAX_CANDIDATES = 64
RETENTION_SECONDS = 86400


def check_candidate_quota(count: int) -> None:
    # Each inspected snapshot is already capped at 16 MiB: <= 1 GiB total.
    if count >= MAX_CANDIDATES:
        from services.plugins.lifecycle import PluginStateError

        raise PluginStateError(
            "Plugin candidate quota reached; uninstall unused candidates first"
        )


def cleanup_orphans(
    root: Path, tracked: set[str], *, now: float | None = None
) -> list[str]:
    """Recover crash leftovers only when old, untracked and not leased.

    A shared stage lock protects snapshots between copy and SQLite commit.
    Download leases are held for the whole download+stage operation.
    """
    cutoff = (time.time() if now is None else now) - RETENTION_SECONDS
    removed = []
    with (root / "stage.lock").open("a") as stage_lock:
        try:
            fcntl.flock(stage_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return []
        for path in root.iterdir():
            if path.is_symlink() or not path.is_dir() or path.name in tracked:
                continue
            download = path.name.startswith("download-")
            try:
                candidate = str(uuid.UUID(path.name)) == path.name
            except ValueError:
                candidate = False
            if not (download or candidate) or path.stat().st_mtime >= cutoff:
                continue
            lease_path = path / ".lease"
            if lease_path.is_symlink():
                continue
            try:
                with lease_path.open("a") as lease:
                    try:
                        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    shutil.rmtree(path)
                    removed.append(path.name)
            except FileNotFoundError:
                continue
    return removed
