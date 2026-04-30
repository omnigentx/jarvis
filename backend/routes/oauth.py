"""OAuth web-flow API — ``/api/oauth/google/*``.

Drives the interactive consent flow for Gmail + Calendar from the Settings
UI.  All endpoints require the bearer token; the flow itself happens on
Google's domain, we just coordinate before/after.

Shape of the frontend contract
------------------------------

1. **Start**: UI calls ``POST /start`` with the ``redirect_uri`` it will
   use.  We generate a nonce ``state``, persist it in the session (well,
   in process memory here — the server is single-user and single-process),
   and return the consent URL.  UI opens that URL in a popup.
2. **Callback**: the popup lands on the redirect URI, scrapes
   ``code`` + ``state`` from the query string, posts them to
   ``POST /callback`` on the main window, then closes.  We verify the
   state, exchange the code, and store the tokens.
3. **Status / Disconnect**: plain CRUD on the stored tokens.
"""
from __future__ import annotations

import logging
import secrets as _secrets
import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import verify_api_key
from services import google_oauth

logger = logging.getLogger("oauth_api")
router = APIRouter(prefix="/api/oauth/google", tags=["oauth"])


# ---- In-process state nonce store -------------------------------------------
#
# For a single-user backend a tiny in-memory dict is fine.  We keep the
# redirect_uri alongside the state so the callback verifies that the exchange
# target matches what we advertised during /start.
_state_lock = threading.Lock()
_pending_states: dict[str, tuple[str, float]] = {}
_STATE_TTL = 10 * 60  # 10 minutes


def _register_state(redirect_uri: str) -> str:
    state = _secrets.token_urlsafe(24)
    now = time.time()
    with _state_lock:
        # Opportunistic sweep so the dict doesn't grow unbounded if the user
        # abandons the flow repeatedly.
        for k, (_uri, ts) in list(_pending_states.items()):
            if now - ts > _STATE_TTL:
                _pending_states.pop(k, None)
        _pending_states[state] = (redirect_uri, now)
    return state


def _consume_state(state: str) -> Optional[str]:
    with _state_lock:
        entry = _pending_states.pop(state, None)
    if entry is None:
        return None
    redirect_uri, created_at = entry
    if time.time() - created_at > _STATE_TTL:
        return None
    return redirect_uri


# ---- Schemas ----------------------------------------------------------------


class ClientConfig(BaseModel):
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: str = Field(min_length=1, max_length=500)
    client_type: str = Field(pattern="^(desktop|web)$")


class StartBody(BaseModel):
    # Required for ``web`` clients (the UI knows its own origin). For
    # ``desktop`` clients the backend forces ``DESKTOP_LOOPBACK_REDIRECT_URI``
    # so Google's loopback flow always matches — any value the UI sends is
    # ignored in that mode.
    redirect_uri: Optional[str] = Field(default=None, max_length=500)


class CallbackBody(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=128)


# ---- Endpoints --------------------------------------------------------------


@router.get("/status", dependencies=[Depends(verify_api_key)])
async def status():
    client = google_oauth.load_client()
    tokens = google_oauth.load_tokens()
    ctype = google_oauth.client_type()
    return {
        "client_configured": client is not None,
        "client_type": ctype,
        "desktop_redirect_uri": (
            google_oauth.DESKTOP_LOOPBACK_REDIRECT_URI if ctype == "desktop" else None
        ),
        "connected": tokens is not None,
        "scopes": list(tokens.scopes) if tokens else [],
        "expires_at": tokens.expires_at if tokens else None,
        "has_refresh_token": bool(tokens and tokens.refresh_token),
        # Cloud project identifier parsed from the stored client_id, so the
        # UI can deep-link the user into the right project for "Enable API".
        "project_number": google_oauth.project_number(),
        "required_apis": google_oauth.required_api_links(),
    }


@router.put("/client", dependencies=[Depends(verify_api_key)])
async def set_client(payload: ClientConfig):
    try:
        google_oauth.save_client(
            payload.client_id.strip(),
            payload.client_secret.strip(),
            payload.client_type,  # type: ignore[arg-type]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"saved": True}


@router.delete("/client", dependencies=[Depends(verify_api_key)])
async def clear_client():
    """Drop the stored client so the Settings form reappears."""
    google_oauth.clear_client()
    return {"cleared": True, "client_type": google_oauth.client_type()}


@router.post("/start", dependencies=[Depends(verify_api_key)])
async def start(payload: StartBody):
    ctype = google_oauth.client_type()
    if ctype == "desktop":
        # Desktop-app clients are registered in Google Cloud with the loopback
        # redirect. Force the canonical value so the token exchange later
        # matches what Google verified during consent.
        effective_uri = google_oauth.DESKTOP_LOOPBACK_REDIRECT_URI
    elif ctype == "web":
        if not payload.redirect_uri or not payload.redirect_uri.strip():
            raise HTTPException(
                status_code=400,
                detail="redirect_uri is required when using a web-application client.",
            )
        effective_uri = payload.redirect_uri.strip()
    else:
        raise HTTPException(
            status_code=400,
            detail="Google OAuth client not configured — save credentials via Settings.",
        )

    state = _register_state(effective_uri)
    try:
        url = google_oauth.build_consent_url(
            redirect_uri=effective_uri, state=state
        )
    except RuntimeError as exc:
        # Roll back the state we just registered so /status doesn't lie.
        with _state_lock:
            _pending_states.pop(state, None)
        raise HTTPException(status_code=400, detail=str(exc))
    return {"url": url, "state": state, "redirect_uri": effective_uri, "client_type": ctype}


@router.post("/callback", dependencies=[Depends(verify_api_key)])
async def callback(payload: CallbackBody):
    redirect_uri = _consume_state(payload.state)
    if redirect_uri is None:
        raise HTTPException(
            status_code=400,
            detail="Unknown or expired OAuth state — restart the flow.",
        )
    try:
        tokens = google_oauth.exchange_code(
            code=payload.code, redirect_uri=redirect_uri
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "connected": True,
        "scopes": list(tokens.scopes),
        "has_refresh_token": bool(tokens.refresh_token),
    }


@router.delete("", dependencies=[Depends(verify_api_key)])
async def disconnect():
    google_oauth.revoke()
    return {"disconnected": True}
