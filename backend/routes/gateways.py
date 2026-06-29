"""HTTP routes for the messaging-gateways Settings panel.

Config read/write goes through the generic ``/api/settings`` endpoints
(category ``gateways``) — those already handle encryption, history, and the
change events that trigger live reload. This module only adds the two things
settings can't: live runtime **status** and a **test-connection** probe.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import verify_api_key
from services.gateways.registry import GATEWAY_REGISTRY

logger = logging.getLogger("gateways_api")
router = APIRouter(prefix="/api/gateways", tags=["gateways"])


class TestBody(BaseModel):
    # Optional: when omitted/blank, the already-saved token is tested (so the
    # user can re-verify a stored token without re-typing it).
    token: str | None = Field(default=None, max_length=500)


@router.get("", dependencies=[Depends(verify_api_key)])
async def gateway_status():
    """Per-platform config + live status (enabled/running/connected/bot/error)."""
    import services.shared_state as state

    mgr = getattr(state, "gateway_manager", None)
    if mgr is None:
        # Manager not started (e.g. very early boot) — report registered
        # platforms as all-off so the UI still renders.
        return {"gateways": [
            {"platform": name, "enabled": False, "running": False,
             "connected": False, "bot_username": None, "last_error": None,
             "agent": "Jarvis", "allow_count": 0, "has_token": False}
            for name in GATEWAY_REGISTRY
        ]}
    return {"gateways": mgr.status()}


@router.post("/{platform}/test", dependencies=[Depends(verify_api_key)])
async def test_connection(platform: str, body: TestBody):
    """Validate a bot token via the platform's ``getMe``.

    Tests the token in the request body, or — when none is given — the token
    already saved for this platform (so the user can re-verify without
    re-typing the secret).
    """
    cls = GATEWAY_REGISTRY.get(platform)
    if cls is None:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {platform!r}")
    probe = getattr(cls, "probe", None)
    if probe is None:
        raise HTTPException(status_code=400, detail=f"{platform} does not support token testing")

    token = (body.token or "").strip()
    if not token:
        # Fall back to the stored token (soft-fail on a stale/undecryptable row).
        from services.config_service import config_service
        from services.secret_utils import safe_get_or_none
        token = (safe_get_or_none(config_service, "gateways", f"{platform}_token") or "").strip()
    if not token:
        return {"ok": False, "error": "No token to test — enter one first."}

    return await probe(token)
