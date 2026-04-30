"""Auth routes: login, check."""
import logging

from fastapi import APIRouter, Request, Depends, HTTPException, status

from core import auth as core_auth
from core.auth import verify_api_key, _check_rate_limit, record_login_attempt

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
async def login(request: Request):
    """Login with API key (password). Returns token for frontend storage."""
    client_ip = request.client.host if request.client else "unknown"

    if not _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again later."
        )

    # Read through the module so updates via the Setup Wizard / Settings API
    # are picked up without a restart.
    current_key = core_auth.JARVIS_API_KEY
    if not current_key:
        return {"token": "", "status": "ok", "message": "No auth configured (dev mode)"}

    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid request body")

    password = data.get("password", "")
    record_login_attempt(client_ip)

    if password == current_key:
        logger.info(f"[AUTH] Login success from {client_ip}")
        return {"token": current_key, "status": "ok"}

    logger.warning(f"[AUTH] Failed login attempt from {client_ip}")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid password"
    )


@router.get("/check")
async def check_auth(_=Depends(verify_api_key)):
    """Check if current auth token is valid."""
    return {"status": "ok", "authenticated": True}
