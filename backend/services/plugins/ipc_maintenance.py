"""Remove abandoned IPC inodes after crashes; never unlink a live endpoint."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import time
from pathlib import Path


async def cleanup_sockets(root: Path, *, now: float | None = None) -> list[str]:
    cutoff = (time.time() if now is None else now) - 86400
    removed = []
    for path in root.iterdir():
        if path.is_symlink() or not re.fullmatch(r"[a-f0-9]{24}\.sock", path.name):
            continue
        before = path.stat()
        if (
            before.st_uid != os.getuid()
            or not stat.S_ISSOCK(before.st_mode)
            or before.st_mtime >= cutoff
        ):
            continue
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(path), 0.2
            )
        except (ConnectionRefusedError, FileNotFoundError):
            try:
                if not path.is_symlink() and path.stat().st_ino == before.st_ino:
                    path.unlink()
                    removed.append(path.name)
            except FileNotFoundError:
                pass
        except (TimeoutError, PermissionError):
            continue
        else:
            writer.close()
            await writer.wait_closed()
    return removed
