"""Authentication module — single-tenant API key + signed-cookie session.

Two paths are accepted:

* **Session cookie** (``jarvis_session``) — minted by ``POST /api/auth/login``
  and verified per request via :func:`core.session.verify_session_token`.
  This is the primary path used by the dashboard.
* **Bearer token / query ``?api_key=``** — legacy transition surface.
  Programmatic clients (Xiaozhi, scripts, EventSource on older builds)
  still use these; we keep them working but the dashboard no longer
  emits either.  They are rate-limited identically.

When ``JARVIS_API_KEY`` is empty (dev / fresh install pre-Setup) auth is
open — the Setup-Gate middleware blocks the API surface anyway.
"""
import logging
import os
import time

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Single API key for all access (web login, Xiaozhi, programmatic).
JARVIS_API_KEY = os.getenv("JARVIS_API_KEY", "")

# Rate limiting state (simple in-memory, per-IP).
_login_attempts: dict[str, list[float]] = {}
LOGIN_RATE_LIMIT = 5      # max attempts
LOGIN_RATE_WINDOW = 60    # per N seconds

# Bearer token extractor (auto_error=False so we can fall back to cookie).
security = HTTPBearer(auto_error=False)


def _check_rate_limit(client_ip: str) -> bool:
    """Returns True if the client is still under the login attempt budget."""
    now = time.time()
    attempts = _login_attempts.get(client_ip, [])
    attempts = [t for t in attempts if now - t < LOGIN_RATE_WINDOW]
    _login_attempts[client_ip] = attempts
    return len(attempts) < LOGIN_RATE_LIMIT


def record_login_attempt(client_ip: str) -> None:
    attempts = _login_attempts.get(client_ip, [])
    attempts.append(time.time())
    _login_attempts[client_ip] = attempts


def _verify_session_cookie(request: Request) -> bool:
    """Try to authenticate via the ``jarvis_session`` cookie.

    Returns True on success.  Returns False (NOT raises) on any failure
    so the caller can fall back to Bearer / query auth before deciding
    to 401.
    """
    from core.session import SESSION_COOKIE_NAME, SessionVerifyError, verify_session_token

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return False
    try:
        verify_session_token(token)
        return True
    except SessionVerifyError as exc:
        # A bad cookie should not silently mask a working Bearer header
        # (that would block automation), so we just log and let the
        # caller try the legacy paths.
        logger.debug("[AUTH] Session cookie rejected: %s", exc.reason)
        return False


async def verify_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> bool:
    """FastAPI dependency.  Accepts session cookie, Bearer token, or
    legacy ``?api_key=`` query param.

    * Session cookie path is the primary dashboard surface.
    * Bearer / query are kept for programmatic clients and EventSource
      callers from older builds.
    """
    if not JARVIS_API_KEY:
        return True  # dev mode

    if _verify_session_cookie(request):
        return True

    # Legacy fallbacks.  Both compared against the live module global so
    # an in-process key rotation takes effect immediately.
    query_key = request.query_params.get("api_key")
    if query_key and query_key == JARVIS_API_KEY:
        return True
    if credentials and credentials.credentials == JARVIS_API_KEY:
        return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "unauthorized", "reason": "invalid_or_missing_credentials"},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def verify_optional_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> bool:
    """Same as :func:`verify_api_key` but never raises.  Used by routes
    that want to behave differently for authed vs anon callers (e.g.
    GET ``/api/setup/auth/probe``)."""
    if not JARVIS_API_KEY:
        return True

    if _verify_session_cookie(request):
        return True

    query_key = request.query_params.get("api_key")
    if query_key and query_key == JARVIS_API_KEY:
        return True
    if credentials and credentials.credentials == JARVIS_API_KEY:
        return True

    return False
