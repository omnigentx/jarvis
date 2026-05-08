"""Authentication routes — session cookie issuance + lifecycle.

Endpoints
---------

* ``POST /api/auth/login``    — exchange the API key for a signed
                                 ``jarvis_session`` cookie + ``jarvis_csrf``
                                 cookie.  Body: ``{"api_key": "..."}``.
* ``POST /api/auth/logout``   — clear both cookies (idempotent).
* ``POST /api/auth/refresh``  — extend the current session by another
                                 TTL window; preserves the absolute
                                 ceiling (``abs_exp``).
* ``GET  /api/auth/whoami``   — lightweight, returns 200 + session info
                                 for the dashboard's pre-flight probe.
* ``GET  /api/auth/check``    — kept as legacy alias of ``/whoami`` so
                                 older clients don't break.

Cookie attributes
-----------------

* ``jarvis_session``: ``HttpOnly``, ``Secure`` (when prod), ``SameSite=Lax``,
  ``Path=/``, lifetime = session TTL.  HttpOnly stops XSS from reading
  the token; SameSite=Lax stops cross-site form-POST exfiltration.
* ``jarvis_csrf``:    NOT HttpOnly (the SPA must echo it as a header),
  ``Secure`` (when prod), ``SameSite=Lax``, ``Path=/``, lifetime = same.

Audit
-----

Every login attempt and outcome (success / failure / rate-limited) is
logged with a stable ``[AUTH]`` prefix so a future ``grep`` or log
shipper can build a security dashboard without parsing structured
fields.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from core import auth as core_auth
from core.auth import (
    _check_rate_limit,
    record_login_attempt,
    verify_api_key,
)
from core.session import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    SessionVerifyError,
    create_session_token,
    make_csrf_token,
    refresh_session_token,
    verify_session_token,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---- Schemas ----------------------------------------------------------------


class LoginRequest(BaseModel):
    api_key: str = Field(..., min_length=1, max_length=512)


class LoginResponse(BaseModel):
    status: str = "ok"
    csrf_token: str  # echoed in the body so the SPA can store it before the cookie is observable
    expires_in: int  # seconds until session expires (UI hint)


class WhoamiResponse(BaseModel):
    authenticated: bool
    # Time-to-live hints. We deliberately do NOT expose the session id
    # (``sid``) to the dashboard: it would let an XSS exfiltrate a
    # value that could be correlated against server-side audit logs to
    # de-anonymize a session. The backend's structured logs already
    # carry ``sid``; the frontend has no need for it.
    expires_in: Optional[int] = None  # seconds until exp
    abs_expires_in: Optional[int] = None  # seconds until hard ceiling


# ---- Helpers ----------------------------------------------------------------


def _is_secure_request(request: Request) -> bool:
    """Decide whether to mark cookies ``Secure``.

    True when the originating scheme is HTTPS or behind a proxy
    forwarding ``X-Forwarded-Proto: https``.  In dev (``http://localhost``)
    we leave Secure off so the cookies are actually accepted by the
    browser.
    """
    if request.url.scheme == "https":
        return True
    fwd = request.headers.get("x-forwarded-proto", "")
    return "https" in fwd.lower()


def _set_auth_cookies(
    response: Response,
    request: Request,
    session_token: str,
    csrf_token: str,
) -> None:
    """Set both cookies with consistent attributes."""
    secure = _is_secure_request(request)
    common = {
        "max_age": SESSION_TTL_SECONDS,
        "httponly": True,
        "secure": secure,
        "samesite": "lax",
        "path": "/",
    }
    response.set_cookie(SESSION_COOKIE_NAME, session_token, **common)
    # CSRF cookie is readable by JS — it's the "double-submit" half.
    response.set_cookie(
        CSRF_COOKIE_NAME, csrf_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=False,  # SPA must read this to echo as X-CSRF-Token
        secure=secure,
        samesite="lax",
        path="/",
    )


def _clear_auth_cookies(response: Response, request: Request) -> None:
    secure = _is_secure_request(request)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", secure=secure, samesite="lax")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/", secure=secure, samesite="lax")


# ---- Routes -----------------------------------------------------------------


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, request: Request, response: Response) -> LoginResponse:
    """Exchange the API key for an httpOnly session cookie.

    Rate-limited per source IP at :data:`core.auth.LOGIN_RATE_LIMIT` per
    :data:`core.auth.LOGIN_RATE_WINDOW` seconds — same budget as the
    legacy login endpoint to keep brute-force attempts bounded across
    paths.
    """
    client_ip = request.client.host if request.client else "unknown"

    if not _check_rate_limit(client_ip):
        logger.warning("[AUTH] Rate-limited login attempt from %s", client_ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited", "reason": "too_many_login_attempts"},
        )

    record_login_attempt(client_ip)

    expected = core_auth.JARVIS_API_KEY
    if not expected:
        # Dev-mode / pre-Setup: refuse to mint sessions.  Without an
        # expected key, we can't *verify* anyone, so handing out a
        # session would be free auth.  Tell the caller to finish setup.
        logger.error("[AUTH] login called before JARVIS_API_KEY is configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "not_configured", "reason": "auth_key_unset"},
        )

    if payload.api_key.strip() != expected:
        logger.warning("[AUTH] Login failed from %s — wrong api_key", client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "reason": "invalid_credentials"},
        )

    try:
        session_token, parsed = create_session_token()
    except RuntimeError as exc:
        # Most likely cause: JWT_SECRET missing.  Surface as 503 — the
        # backend cannot mint sessions until this is fixed.
        logger.error("[AUTH] Cannot mint session: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "not_configured", "reason": "jwt_secret_unset"},
        ) from exc

    csrf_token = make_csrf_token()
    _set_auth_cookies(response, request, session_token, csrf_token)

    logger.info("[AUTH] Login success from %s sid=%s", client_ip, parsed["sid"])
    return LoginResponse(
        csrf_token=csrf_token,
        expires_in=SESSION_TTL_SECONDS,
    )


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    """Clear both cookies.  Idempotent: returns 200 even when not logged in."""
    sid = "<none>"
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token:
        try:
            payload = verify_session_token(raw_token)
            sid = payload["sid"]
        except SessionVerifyError:
            pass

    client_ip = request.client.host if request.client else "unknown"
    logger.info("[AUTH] Logout from %s sid=%s", client_ip, sid)
    _clear_auth_cookies(response, request)
    return {"status": "ok"}


@router.post("/refresh", response_model=LoginResponse)
async def refresh(request: Request, response: Response) -> LoginResponse:
    """Extend the current session by another TTL window.

    Refusal modes (so the dashboard can branch):

    * 401 ``expired``                — silent-refresh window has lapsed; user must re-login.
    * 401 ``max_lifetime_exceeded``  — hard ceiling reached; re-login required.
    * 401 ``key_rotated``            — operator rotated the API key.
    * 401 ``invalid_signature``      — token tampered or JWT_SECRET changed.
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "reason": "no_session"},
        )

    try:
        new_token, parsed = refresh_session_token(raw_token)
    except SessionVerifyError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "reason": exc.reason},
        ) from exc

    csrf_token = make_csrf_token()
    _set_auth_cookies(response, request, new_token, csrf_token)
    logger.info("[AUTH] Session refreshed sid=%s", parsed["sid"])
    return LoginResponse(csrf_token=csrf_token, expires_in=SESSION_TTL_SECONDS)


@router.get("/whoami", response_model=WhoamiResponse)
async def whoami(request: Request) -> WhoamiResponse:
    """Lightweight pre-flight probe.

    Unlike :func:`verify_api_key`, this never raises — it returns
    ``authenticated: False`` so the dashboard can decide whether to show
    the AuthGate modal *without* triggering a 401 response (which would
    pollute network logs and might confuse error reporters).
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        return WhoamiResponse(authenticated=False)

    try:
        payload = verify_session_token(raw_token)
    except SessionVerifyError:
        return WhoamiResponse(authenticated=False)

    import time as _time
    now = int(_time.time())
    return WhoamiResponse(
        authenticated=True,
        expires_in=max(0, int(payload["exp"]) - now),
        abs_expires_in=max(0, int(payload["abs_exp"]) - now),
    )


@router.get("/check")
async def check_auth(_=Depends(verify_api_key)) -> dict:
    """Legacy alias for ``/whoami``.  Some scripts hit this for a 200/401
    pulse.  Kept for compatibility; new clients use ``/whoami``."""
    return {"status": "ok", "authenticated": True}
