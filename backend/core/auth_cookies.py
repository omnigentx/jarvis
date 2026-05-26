"""Shared cookie-setting helpers for the auth routes.

Lives outside ``routes/auth.py`` so other route modules that need to
mint or clear the session cookies (the setup wizard, the future passkey
re-auth path) can import a stable, public symbol instead of reaching
into ``routes.auth`` for an underscore-prefixed function.

The previous ``routes.auth._set_auth_cookies`` lived in the file that
*also* owns the login route — fine within ``routes/auth.py``, but a
cross-module private-symbol import (``from routes.auth import
_set_auth_cookies``) breaks silently when the source module changes
its signature. Moving the helper here makes the cookie contract
intentionally part of the public API surface.
"""
from __future__ import annotations

from fastapi import Request, Response

from core.session import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, SESSION_TTL_SECONDS


def is_secure_request(request: Request) -> bool:
    """Decide whether to mark cookies ``Secure``.

    True when the originating scheme is HTTPS or behind a proxy
    forwarding ``X-Forwarded-Proto: https``. In dev (``http://localhost``)
    we leave Secure off so the cookies are actually accepted by the
    browser.
    """
    if request.url.scheme == "https":
        return True
    fwd = request.headers.get("x-forwarded-proto", "")
    return "https" in fwd.lower()


def set_auth_cookies(
    response: Response,
    request: Request,
    session_token: str,
    csrf_token: str,
) -> None:
    """Set both auth cookies with consistent attributes.

    Used by ``POST /api/auth/login``, ``POST /api/auth/refresh``,
    ``POST /api/auth/passkey/authenticate/finish``, and
    ``POST /api/setup/auth`` — anywhere the backend hands a fresh
    session to the SPA.
    """
    secure = is_secure_request(request)
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
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_auth_cookies(response: Response, request: Request) -> None:
    secure = is_secure_request(request)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", secure=secure, samesite="lax")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/", secure=secure, samesite="lax")
