"""System API — ``/api/system/*``.

Operational endpoints that don't belong to a specific feature:

* ``POST /api/system/restart`` — request a graceful backend restart.  The
  process exits with status 0 so whichever supervisor is running us
  (docker-compose ``restart: unless-stopped``, systemd, PM2, etc.) can
  bring it back cleanly.  If you're running standalone (``uv run ...``)
  the backend will simply exit and you'll need to restart it manually.

We do *not* attempt an in-process re-exec: FastAgent's MCP subprocesses,
scheduler tasks, and socket servers are not safe to hot-swap, and
restarting the OS process is the only way to get a truly clean slate.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from fastapi import APIRouter, Depends

from core.auth import verify_api_key

logger = logging.getLogger("system_api")
router = APIRouter(prefix="/api/system", tags=["system"])


async def _trigger_exit(delay: float) -> None:
    """Send SIGTERM to ourselves after returning the HTTP response."""
    await asyncio.sleep(delay)
    logger.warning("[SYSTEM] Restart requested — sending SIGTERM to self (pid=%d)", os.getpid())
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception:
        logger.exception("[SYSTEM] Restart signal failed; forcing os._exit")
        os._exit(0)


@router.post("/restart", dependencies=[Depends(verify_api_key)])
async def restart():
    """Schedule a SIGTERM so the process manager restarts us.

    We return **before** the signal fires so the client sees the 202 and
    can show a "restarting…" toast.  The 0.5s delay gives uvicorn enough
    time to flush the response over the socket.
    """
    asyncio.create_task(_trigger_exit(0.5))
    return {"restarting": True, "pid": os.getpid()}
