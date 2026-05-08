"""CSRF protection (double-submit cookie pattern).

How it works
------------

1. ``POST /api/auth/login`` succeeds → backend sets two cookies:

   * ``jarvis_session`` — httpOnly, signed (see :mod:`core.session`).
   * ``jarvis_csrf``    — readable from JS, random per session.

2. The dashboard reads ``jarvis_csrf`` and echoes the value back in an
   ``X-CSRF-Token`` header on every state-changing request.
3. This middleware fails any mutation whose header is missing or does
   not match the cookie value.

Why double-submit and not synchronizer-token?
---------------------------------------------

We're stateless on purpose (no DB lookup per request).  Same-Site=Lax
on the cookies blocks the basic cross-site form-POST attack; the
header check kills the remaining ``<img src=...>`` / fetch
no-cors / cross-origin XHR vectors that can plant a request but cannot
read the cookie value.

Scope of protection
-------------------

This middleware ONLY defends **cookie-authenticated** callers. The
double-submit pattern works by checking that a request's
``X-CSRF-Token`` header matches the ``jarvis_csrf`` cookie — a value
the browser will auto-attach but a third-party origin cannot read.

**Bearer / query-param callers (Xiaozhi, automation scripts) carry no
CSRF cookie**, so they are intentionally allowed through
this middleware. They have a different threat model: the caller owns
the long-lived API key, so request-forgery in the browser sense
doesn't apply. Their own client code is responsible for not handing
the key to untrusted JS.

If a future change tightens this — e.g., requiring CSRF on Bearer
mutations too — Xiaozhi and any custom automation will start
returning 403. That's a breaking change; coordinate with the
programmatic-client owners first.

Exemptions
----------

* ``GET / HEAD / OPTIONS`` are always allowed.
* The auth bootstrap endpoints (``/api/auth/login``, ``/api/setup/...``)
  are exempt because the user has not yet been issued a CSRF cookie at
  that point.  ``/api/auth/login`` itself is rate-limited per-IP and
  same-origin enforced by SameSite=Lax on the response Set-Cookie.
* OAuth callback endpoints (``/api/oauth/.../callback``) are exempt —
  third-party redirect cannot carry our header.  Those routes have
  their own ``state``-parameter CSRF defence.
"""
from __future__ import annotations

import hmac
import json
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

from core.session import CSRF_COOKIE_NAME

logger = logging.getLogger(__name__)


# Methods that require CSRF.  GET and HEAD are safe (RFC 7231 §4.2.1)
# and OPTIONS is the CORS preflight.
_PROTECTED_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Path prefixes that skip CSRF entirely.  Keep this list minimal.
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/auth/login",       # user has no CSRF cookie yet
    "/api/auth/logout",      # safe to invoke without prior cookie (no side effect on logout)
    "/api/setup",            # bootstrap surface; user has no CSRF cookie until wizard ends
    "/api/oauth/google/callback",  # third-party redirect; has its own `state` CSRF
)


def _is_exempt(path: str) -> bool:
    for prefix in _EXEMPT_PREFIXES:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            return True
    return False


def _parse_cookies(scope: Scope) -> dict[str, str]:
    raw = ""
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            raw = value.decode("latin-1")
            break
    if not raw:
        return {}
    out: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            k, _, v = part.strip().partition("=")
            out[k] = v
    return out


def _get_header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode("latin-1")
    for hname, hval in scope.get("headers", []):
        if hname == target:
            return hval.decode("latin-1")
    return None


class CsrfMiddleware:
    """Pure-ASGI CSRF guard. Rejects mismatched mutations with 403.

    Implemented at the ASGI layer (not :class:`BaseHTTPMiddleware`) for
    the same reason as :class:`SetupGateMiddleware`: SSE responses must
    not be buffered through Starlette's stream wrapper.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method: str = scope.get("method", "GET").upper()
        if method not in _PROTECTED_METHODS:
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "") or ""
        # Only guard /api/* — the SPA itself is static and submits forms
        # back into /api/.
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        if _is_exempt(path):
            await self.app(scope, receive, send)
            return

        cookies = _parse_cookies(scope)
        cookie_token = cookies.get(CSRF_COOKIE_NAME, "")
        header_token = _get_header(scope, "x-csrf-token") or ""

        # No CSRF cookie → caller is either unauthenticated (route's
        # auth dep will 401 it) or using Bearer / query-param auth
        # (programmatic clients, see module docstring "Scope of
        # protection"). Both cases legitimately have no cookie to
        # check; pass through.
        if not cookie_token:
            await self.app(scope, receive, send)
            return

        if not header_token or not hmac.compare_digest(cookie_token, header_token):
            logger.warning(
                "[CSRF] Reject %s %s — header=%r cookie=%r",
                method, path,
                _redact(header_token), _redact(cookie_token),
            )
            await self._reject(send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = json.dumps({
            "error": "csrf_failed",
            "detail": "Missing or invalid CSRF token. Re-load the page and retry.",
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})


def _redact(token: str) -> str:
    """Show only first 4 chars in logs so a leaked log file can't be
    used to forge the next request."""
    if not token:
        return "<empty>"
    if len(token) <= 4:
        return "***"
    return token[:4] + "***"
