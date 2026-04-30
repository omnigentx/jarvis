"""HTTP middleware that blocks API traffic until the Setup Wizard is done.

While the wizard is incomplete, any authenticated or anonymous request targeting
an API endpoint other than the wizard/auth bootstrap surface is answered with
``503 Service Unavailable`` + ``X-Setup-Required: true``.  The frontend uses
this signal to redirect the browser to ``/#/setup``.

Non-API paths are always allowed: the SPA needs to fetch its own static assets
to actually render the wizard UI.  OpenAPI surfaces are also always open so the
operator can inspect the API even mid-setup.
"""
from __future__ import annotations

import json
import logging
import threading

from starlette.types import ASGIApp, Receive, Scope, Send

from core.database import (
    SETUP_WIZARD_CRITICAL_STEPS,
    SetupWizardStep,
    get_db_session,
)

logger = logging.getLogger(__name__)

# Paths that must always be reachable, regardless of setup state.
# ``/api/yaml`` is listed so Step 4 of the wizard can load and save the
# fast-agent YAML files; the endpoints themselves still require the master
# API key via ``verify_api_key``.
_ALLOWED_PREFIXES: tuple[str, ...] = (
    "/api/setup",
    "/api/auth",
    "/api/yaml",
    # /api/oauth is needed during the wizard's Step 3 — External Services
    # calls /api/oauth/google/status to render the Connect Google button,
    # and POST /api/oauth/google/{start,callback} to complete consent. The
    # endpoints themselves still require verify_api_key.
    "/api/oauth",
    "/docs",
    "/redoc",
    "/openapi.json",
)


# ---- Cached status ----------------------------------------------------------

_cache_lock = threading.Lock()
_setup_complete: bool | None = None


def _compute_from_db() -> bool:
    """Run the `overall_complete` rule against the live DB."""
    db = get_db_session()
    try:
        rows = {r.step_name: r for r in db.query(SetupWizardStep).all()}
    finally:
        db.close()
    for critical in SETUP_WIZARD_CRITICAL_STEPS:
        r = rows.get(critical)
        if r is None or not bool(r.completed):
            return False
    return True


def refresh_setup_complete() -> bool:
    """Re-read wizard state from DB and cache it.  Call after any mutation."""
    global _setup_complete
    try:
        fresh = _compute_from_db()
    except Exception:
        # If the DB is unavailable we fail closed: treat setup as incomplete
        # rather than accidentally unlocking the API.
        logger.exception("[SETUP_GATE] Failed to read wizard state; assuming incomplete")
        fresh = False
    with _cache_lock:
        _setup_complete = fresh
    return fresh


def is_setup_complete() -> bool:
    with _cache_lock:
        cached = _setup_complete
    if cached is None:
        return refresh_setup_complete()
    return cached


def _reset_cache_for_tests() -> None:
    """Test-only helper; clears the in-memory latch."""
    global _setup_complete
    with _cache_lock:
        _setup_complete = None


# ---- Middleware -------------------------------------------------------------


class SetupGateMiddleware:
    """Pure-ASGI gate middleware.

    Starlette's ``BaseHTTPMiddleware`` wraps the downstream response through
    an internal memory stream which materially delays ``StreamingResponse``
    chunks — including ``EventSourceResponse``. Implementing the middleware
    at the raw ASGI layer keeps the send pipeline unbuffered so SSE events
    reach the browser as soon as the handler yields them.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "") or ""

        # Non-API paths — serve the SPA, static files, etc.
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        # Bootstrap surfaces stay open even when incomplete.
        for prefix in _ALLOWED_PREFIXES:
            if path == prefix or path.startswith(prefix + "/"):
                await self.app(scope, receive, send)
                return

        if is_setup_complete():
            await self.app(scope, receive, send)
            return

        body = json.dumps({
            "error": "setup_required",
            "detail": (
                "Jarvis is not configured yet. Complete the Setup Wizard "
                "at /#/setup (or POST /api/setup/...)."
            ),
            "redirect": "/#/setup",
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-setup-required", b"true"),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})
