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
from sqlalchemy.orm import Session as DbSession

from core import webauthn as wa
from core.auth_cookies import (
    clear_auth_cookies,
    is_secure_request,
    set_auth_cookies,
)
from core.database import (
    DEFAULT_USERNAME,
    DEFAULT_USER_ID,
    PasskeyCredential,
    User,
    get_db,
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


# Cookie helpers were lifted to ``core/auth_cookies.py`` so the setup
# wizard (and any future cookie-minting route) can import them by their
# public names instead of from this module's underscore-prefixed namespace.
# These shim names preserve call sites within this file.
_is_secure_request = is_secure_request
_set_auth_cookies = set_auth_cookies
_clear_auth_cookies = clear_auth_cookies


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


# ---- Passkey (WebAuthn) -----------------------------------------------------
#
# Browser-side login UX. Coexists with the legacy API key (which remains
# the script/Xiaozhi credential and the recovery path). See
# ``backend/core/webauthn.py`` for the wrapper and the recovery doc in
# the README.


class PasskeyRegisterBeginResponse(BaseModel):
    ceremony_id: str
    options: dict  # PublicKeyCredentialCreationOptions, JSON-shaped


class PasskeyRegisterFinishRequest(BaseModel):
    ceremony_id: str
    credential: dict  # navigator.credentials.create() result, JSON-shaped
    label: Optional[str] = Field(default=None, max_length=100)


class PasskeyCredentialOut(BaseModel):
    id: str
    label: Optional[str] = None
    rp_id: str
    created_at: float
    last_used_at: Optional[float] = None
    transports: list[str] = Field(default_factory=list)


class PasskeyAuthenticateBeginResponse(BaseModel):
    ceremony_id: str
    options: dict


class PasskeyAuthenticateFinishRequest(BaseModel):
    ceremony_id: str
    credential: dict  # navigator.credentials.get() result, JSON-shaped


class PasskeyHasAnyResponse(BaseModel):
    has_passkey: bool


def _user_credentials_for_rp(
    db: DbSession, user_id: str, rp_id: str,
) -> list[PasskeyCredential]:
    return (
        db.query(PasskeyCredential)
        .filter(PasskeyCredential.user_id == user_id)
        .filter(PasskeyCredential.rp_id == rp_id)
        .order_by(PasskeyCredential.created_at.desc())
        .all()
    )


def _decode_transports(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    import json as _json
    try:
        value = _json.loads(raw)
        return [str(t) for t in value] if isinstance(value, list) else []
    except Exception:
        return []


def _serialize_credential(cred: PasskeyCredential) -> PasskeyCredentialOut:
    return PasskeyCredentialOut(
        id=cred.id,
        label=cred.label,
        rp_id=cred.rp_id,
        created_at=cred.created_at,
        last_used_at=cred.last_used_at,
        transports=_decode_transports(cred.transports),
    )


@router.post(
    "/passkey/register/begin",
    response_model=PasskeyRegisterBeginResponse,
)
async def passkey_register_begin(
    request: Request,
    _=Depends(verify_api_key),
    db: DbSession = Depends(get_db),
) -> PasskeyRegisterBeginResponse:
    """Start a register ceremony for the currently-authenticated user.

    Requires existing auth (Bearer key OR session cookie) because the
    server has to know *who* the credential will be bound to. New users
    bootstrap by logging in with ``JARVIS_API_KEY`` from ``.env`` once,
    then register a passkey from this endpoint.
    """
    user = db.query(User).filter(User.id == DEFAULT_USER_ID).first()
    if user is None:
        # init_db() should have seeded this row; if missing the deploy
        # is corrupted and we fail loud rather than silently fall back.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "default_user_missing"},
        )

    rp_id = wa.rp_id_from_request(request)
    existing = [c.id for c in _user_credentials_for_rp(db, user.id, rp_id)]
    ceremony_id, options = wa.build_registration_options(
        request=request,
        user_id=user.id,
        username=user.username,
        existing_credential_ids=existing,
    )
    return PasskeyRegisterBeginResponse(
        ceremony_id=ceremony_id, options=options,
    )


@router.post("/passkey/register/finish")
async def passkey_register_finish(
    payload: PasskeyRegisterFinishRequest,
    request: Request,
    _=Depends(verify_api_key),
    db: DbSession = Depends(get_db),
) -> dict:
    """Verify the attestation response and persist the new credential."""
    user = db.query(User).filter(User.id == DEFAULT_USER_ID).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "default_user_missing"},
        )

    try:
        verified = wa.verify_registration(
            request=request,
            ceremony_id=payload.ceremony_id,
            credential=payload.credential,
            expected_user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "passkey_register_failed", "reason": str(exc)},
        ) from exc
    except Exception as exc:
        # The library raises subclasses of InvalidRegistrationResponse
        # for malformed attestations; surface as 400 with the message
        # for client-side error rendering. Log at warning since this
        # often indicates browser-side bugs, not infra.
        # Log exception type only — ``str(exc)`` on InvalidRegistrationResponse
        # may contain raw attestation bytes (clientDataJSON, public-key bytes)
        # which is too noisy for production logs and a small information leak.
        logger.warning("[AUTH] passkey register verify failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "passkey_register_failed",
                "reason": "verification_error",
            },
        ) from exc

    credential_id_b64 = wa.b64url_encode(verified.credential_id)
    rp_id = wa.rp_id_from_request(request)

    # transports may arrive as part of the navigator response; the
    # client (passkey.js) is responsible for hoisting it into the
    # payload. We accept it nested either at the top level or under
    # ``response.transports``.
    transports = (
        payload.credential.get("response", {}).get("transports")
        or payload.credential.get("transports")
        or []
    )
    import json as _json
    transports_json = (
        _json.dumps([str(t) for t in transports]) if transports else None
    )

    # Idempotent insert: if the same credential id somehow appears
    # twice (replay of the same finish call, or two simultaneous
    # registers of the same physical authenticator), refresh fields
    # instead of duplicating. The IntegrityError catch closes a race
    # where two finish calls both pass the "existing is None" probe
    # before either commits: the loser of the commit race would hit
    # UniqueConstraintViolation as a 500; instead, fall through to the
    # update branch on retry.
    from sqlalchemy.exc import IntegrityError as _IntegrityError

    def _update_existing(row: PasskeyCredential) -> dict:
        row.user_id = user.id
        row.public_key = bytes(verified.credential_public_key)
        row.sign_count = verified.sign_count
        row.transports = transports_json
        row.rp_id = rp_id
        row.label = payload.label or row.label
        db.commit()
        return {"status": "ok", "credential_id": credential_id_b64, "replaced": True}

    existing = db.query(PasskeyCredential).filter(
        PasskeyCredential.id == credential_id_b64,
    ).first()
    if existing is not None:
        return _update_existing(existing)

    db.add(PasskeyCredential(
        id=credential_id_b64,
        user_id=user.id,
        public_key=bytes(verified.credential_public_key),
        sign_count=verified.sign_count,
        transports=transports_json,
        rp_id=rp_id,
        label=payload.label,
    ))
    try:
        db.commit()
    except _IntegrityError:
        db.rollback()
        # Lost the race — the other request committed our id first.
        # Re-query and update.
        racer_winner = db.query(PasskeyCredential).filter(
            PasskeyCredential.id == credential_id_b64,
        ).first()
        if racer_winner is None:
            # Different constraint failed — re-raise as 400.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "passkey_register_failed",
                    "reason": "integrity_violation",
                },
            )
        return _update_existing(racer_winner)
    logger.info(
        "[AUTH] Passkey registered id=%s rp=%s label=%s",
        credential_id_b64[:12], rp_id, payload.label,
    )
    return {"status": "ok", "credential_id": credential_id_b64, "replaced": False}


@router.get("/passkey/list", response_model=list[PasskeyCredentialOut])
async def passkey_list(
    request: Request,
    _=Depends(verify_api_key),
    db: DbSession = Depends(get_db),
) -> list[PasskeyCredentialOut]:
    """List all passkeys for the current user, scoped to the current RP.

    We deliberately filter by RP ID so the settings page only shows
    credentials usable on the current origin. A passkey registered on
    ``localhost`` doesn't appear when accessed via
    ``jarvis.alice.com`` — that's correct: it physically cannot
    authenticate this session.
    """
    rp_id = wa.rp_id_from_request(request)
    creds = _user_credentials_for_rp(db, DEFAULT_USER_ID, rp_id)
    return [_serialize_credential(c) for c in creds]


@router.get("/passkey/has-any", response_model=PasskeyHasAnyResponse)
async def passkey_has_any(
    request: Request,
    db: DbSession = Depends(get_db),
) -> PasskeyHasAnyResponse:
    """UX probe for AuthGate. Returns ``true`` when the current RP has
    at least one registered passkey, so the SPA can show the "Sign in
    with passkey" button before the user clicks anything.

    Public (no auth) on purpose: pre-login UI can't carry a session
    cookie. The information it leaks is just "this deployment has been
    set up with passkey" — not the credential id, not the user, not
    the key fingerprint. Same shape as ``/whoami`` revealing
    ``authenticated:false``.

    Rate-limited per-IP on the same bucket as /login so a malicious
    client can't spam the DB-touching probe."""
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited"},
        )
    record_login_attempt(client_ip)
    rp_id = wa.rp_id_from_request(request)
    count = (
        db.query(PasskeyCredential)
        .filter(PasskeyCredential.user_id == DEFAULT_USER_ID)
        .filter(PasskeyCredential.rp_id == rp_id)
        .count()
    )
    return PasskeyHasAnyResponse(has_passkey=count > 0)


@router.post(
    "/passkey/authenticate/begin",
    response_model=PasskeyAuthenticateBeginResponse,
)
async def passkey_authenticate_begin(
    request: Request,
) -> PasskeyAuthenticateBeginResponse:
    # See passkey_has_any for rate-limit rationale. Begin allocates
    # in-process ceremony state — without a cap a malicious client
    # can balloon memory by spamming begin without ever finishing.
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited"},
        )
    record_login_attempt(client_ip)
    return await _passkey_authenticate_begin_impl(request)


async def _passkey_authenticate_begin_impl(
    request: Request,
) -> PasskeyAuthenticateBeginResponse:
    """Start a sign-in ceremony. Public (no auth) — this is the
    pre-login step.

    Always passes an EMPTY ``allow_credentials``. Two reasons:

    1. Credentials were registered with ``resident_key=REQUIRED`` so
       the browser can offer them via the discoverable-credential UX
       (Touch ID picker) without the server having to enumerate IDs.
    2. This route is public; echoing back the real credential id list
       would leak it to an unauthenticated probe. The id is not a
       secret per WebAuthn spec but combined with the rp_id it
       uniquely identifies the deployment + an enrolled
       authenticator — useful for an attacker pre-staging device
       fingerprints.

    Verification at ``finish`` time still works because the
    authenticator includes the credential id in the assertion; the
    server looks it up to fetch the public key + sign_count.
    """
    ceremony_id, options = wa.build_authentication_options(
        request=request,
        allow_credential_ids=[],
    )
    return PasskeyAuthenticateBeginResponse(
        ceremony_id=ceremony_id, options=options,
    )


@router.post(
    "/passkey/authenticate/finish",
    response_model=LoginResponse,
)
async def passkey_authenticate_finish(
    payload: PasskeyAuthenticateFinishRequest,
    request: Request,
    response: Response,
    db: DbSession = Depends(get_db),
) -> LoginResponse:
    """Verify the assertion, bump sign_count + last_used_at, mint a
    session cookie (same shape as ``/login`` so the SPA's existing
    auth-store path needs no branch). Public (no auth) on purpose —
    successful assertion *is* the auth."""
    client_ip = request.client.host if request.client else "unknown"

    # Rate-limit on the IP shared with /login. WebAuthn is much harder
    # to brute-force than an API key, but the limit is cheap and stops
    # noisy clients.
    if not _check_rate_limit(client_ip):
        logger.warning(
            "[AUTH] Rate-limited passkey auth attempt from %s", client_ip,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited", "reason": "too_many_login_attempts"},
        )
    record_login_attempt(client_ip)

    # Resolve the credential the browser claims to be using. The
    # WebAuthn ceremony embeds the credential id (base64url) at the
    # top level of the assertion JSON.
    credential_id = payload.credential.get("id") or payload.credential.get("rawId")
    if not credential_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "passkey_auth_failed",
                "reason": "credential_id_missing",
            },
        )

    cred = (
        db.query(PasskeyCredential)
        .filter(PasskeyCredential.id == credential_id)
        .first()
    )
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "passkey_auth_failed",
                "reason": "credential_unknown",
            },
        )

    try:
        verified = wa.verify_authentication(
            request=request,
            ceremony_id=payload.ceremony_id,
            credential=payload.credential,
            credential_public_key=bytes(cred.public_key),
            credential_current_sign_count=cred.sign_count,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "passkey_auth_failed", "reason": str(exc)},
        ) from exc
    except Exception as exc:
        # Type-only — see register_finish for the rationale.
        logger.warning("[AUTH] passkey auth verify failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "passkey_auth_failed",
                "reason": "verification_error",
            },
        ) from exc

    # Bump sign_count and last_used_at AFTER successful verify. If we
    # bumped before and verify failed, a clone would silently consume
    # our counter monotonicity.
    cred.sign_count = verified.new_sign_count
    from datetime import datetime as _dt
    cred.last_used_at = _dt.now().timestamp()
    db.commit()

    # Mint the session cookie — same shape as /login so the SPA reuses
    # the existing post-login codepath without branching.
    try:
        session_token, parsed = create_session_token()
    except RuntimeError as exc:
        logger.error("[AUTH] Cannot mint session after passkey auth: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "not_configured", "reason": "jwt_secret_unset"},
        ) from exc

    csrf_token = make_csrf_token()
    _set_auth_cookies(response, request, session_token, csrf_token)
    logger.info(
        "[AUTH] Passkey login success from %s sid=%s cred=%s",
        client_ip, parsed["sid"], credential_id[:12],
    )
    return LoginResponse(csrf_token=csrf_token, expires_in=SESSION_TTL_SECONDS)


@router.delete("/passkey/{credential_id:path}")
async def passkey_delete(
    credential_id: str,
    _=Depends(verify_api_key),
    db: DbSession = Depends(get_db),
) -> dict:
    """Delete a credential. 404 if it doesn't belong to the default
    user — prevents a curious browser from probing arbitrary ids."""
    cred = (
        db.query(PasskeyCredential)
        .filter(PasskeyCredential.id == credential_id)
        .filter(PasskeyCredential.user_id == DEFAULT_USER_ID)
        .first()
    )
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "passkey_not_found"},
        )
    db.delete(cred)
    db.commit()
    logger.info("[AUTH] Passkey deleted id=%s", credential_id[:12])
    return {"status": "ok"}
