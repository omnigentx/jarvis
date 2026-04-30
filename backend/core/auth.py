"""
Authentication module — Simple API key.
Single-user mode: JARVIS_API_KEY env var serves as both web password and API key.
"""
import logging
import os

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Single API key for all access (web login, Xiaozhi, programmatic)
JARVIS_API_KEY = os.getenv("JARVIS_API_KEY", "")

# Rate limiting state (simple in-memory)
_login_attempts: dict[str, list[float]] = {}  # ip -> [timestamps]
LOGIN_RATE_LIMIT = 5  # max attempts
LOGIN_RATE_WINDOW = 60  # per N seconds

# Bearer token extractor
security = HTTPBearer(auto_error=False)


def _check_rate_limit(client_ip: str) -> bool:
    """Check if client IP has exceeded login rate limit. Returns True if allowed."""
    import time
    now = time.time()
    attempts = _login_attempts.get(client_ip, [])
    # Remove old attempts
    attempts = [t for t in attempts if now - t < LOGIN_RATE_WINDOW]
    _login_attempts[client_ip] = attempts
    return len(attempts) < LOGIN_RATE_LIMIT


def record_login_attempt(client_ip: str):
    """Record a login attempt for rate limiting."""
    import time
    attempts = _login_attempts.get(client_ip, [])
    attempts.append(time.time())
    _login_attempts[client_ip] = attempts


async def verify_api_key(
    request: "Request",
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> bool:
    """
    FastAPI dependency to verify API key.
    - If JARVIS_API_KEY is not set: open access (dev mode)
    - If set: accepts Bearer token OR query param ?api_key=
      (EventSource/SSE cannot send custom headers, so query param is needed)
    """
    if not JARVIS_API_KEY:
        return True  # Dev mode — no auth required
    
    # Support query param for EventSource SSE connections
    query_key = request.query_params.get("api_key")
    if query_key and query_key == JARVIS_API_KEY:
        return True
    
    if credentials and credentials.credentials == JARVIS_API_KEY:
        return True
    
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def verify_optional_api_key(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> bool:
    """
    Optional auth — returns True if authenticated or no key configured.
    Does NOT raise exception for unauthenticated requests.
    """
    if not JARVIS_API_KEY:
        return True
    
    if credentials and credentials.credentials == JARVIS_API_KEY:
        return True
    
    return False
