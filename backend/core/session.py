"""Stateless session-token manager (httpOnly cookie auth).

Tokens are short-lived, HMAC-signed JSON blobs.  The signing secret is
``JWT_SECRET`` (already required by the .env contract), so cycling
``JARVIS_API_KEY`` does NOT invalidate sessions on its own — but we
embed a *key fingerprint* (sha256 of the current API key) in the
payload so any rotation of the auth key auto-invalidates every existing
session at the next request.  No DB writes; no Redis; no extra deps.

Why HMAC-JSON and not pyjwt
---------------------------
The repo already pulls ``cryptography`` transitively, but adding a JWT
library for one signed-cookie use-case is overkill.  ``hmac`` + ``json``
+ ``base64`` from the stdlib are enough and keep the audit surface tiny.

Threat model covered
--------------------

* **Tampering** — payload is signed; constant-time compare on verify.
* **Replay after rotate** — payload contains a sha256 prefix of the
  current ``JARVIS_API_KEY``.  A rotation changes the prefix; old
  sessions stop validating immediately.
* **Replay after expiry** — payload has ``exp`` (UNIX seconds).
* **Replay across deployments** — the signing secret is the deployment
  ``JWT_SECRET``; new deploy with a new secret rejects old tokens.

Out of scope (intentional)
--------------------------

* Server-side revocation list.  We rely on the key-fingerprint trick:
  if an operator needs to kill all sessions, rotate ``JARVIS_API_KEY``.
* Per-IP binding.  Kept off because reverse-proxy deployments often see
  every request from the proxy IP.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Optional, TypedDict

logger = logging.getLogger(__name__)


SESSION_COOKIE_NAME = "jarvis_session"
CSRF_COOKIE_NAME = "jarvis_csrf"

# Default 1 hour. Refresh extends another full TTL.
SESSION_TTL_SECONDS = 60 * 60
# Hard ceiling on a refreshed session. After this many seconds since the
# *original* login, the user must re-authenticate even if they kept
# refreshing. Stops indefinitely-renewable tokens after a token leak.
SESSION_MAX_LIFETIME_SECONDS = 60 * 60 * 12  # 12 hours


class SessionPayload(TypedDict):
    """Shape of the JSON payload inside a session token."""

    iat: int          # issued-at  (UNIX seconds)
    exp: int          # expires-at (UNIX seconds)
    nbf: int          # not-before (UNIX seconds; equals iat for now)
    sid: str          # 128-bit random session id (hex) — ties session ↔ csrf
    kfp: str          # 16-char hex sha256 prefix of current JARVIS_API_KEY
    abs_exp: int      # absolute hard expiry (iat_of_first_login + MAX_LIFETIME)


# ---- Internals --------------------------------------------------------------


def _signing_secret() -> bytes:
    secret = os.environ.get("JWT_SECRET", "")
    if not secret:
        # Refuse to sign anything with a default — callers (login route)
        # should surface a 503 if this is missing rather than mint
        # forgeable tokens.
        raise RuntimeError(
            "JWT_SECRET is not set; refusing to mint session tokens. "
            "Set JWT_SECRET in your environment and restart."
        )
    return secret.encode("utf-8")


def current_key_fingerprint() -> str:
    """16-char hex prefix of sha256(JARVIS_API_KEY).  Rotating the API
    key changes this value; embedding it into the token means the old
    sessions stop validating without server-side state."""
    from core import auth as core_auth
    key = core_auth.JARVIS_API_KEY or ""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return digest[:16]


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload_b64: str) -> str:
    mac = hmac.new(_signing_secret(), payload_b64.encode("ascii"), hashlib.sha256)
    return _b64url_encode(mac.digest())


# ---- Public API -------------------------------------------------------------


def create_session_token(
    *,
    abs_exp: Optional[int] = None,
    now: Optional[int] = None,
) -> tuple[str, SessionPayload]:
    """Mint a session token.

    Parameters
    ----------
    abs_exp:
        When refreshing, pass the existing token's ``abs_exp`` so the
        hard ceiling carries over.  When creating a brand-new session
        (login), leave ``None`` and we set ``iat + SESSION_MAX_LIFETIME``.
    now:
        Test-only override for the current time; production callers
        leave it ``None``.

    Returns
    -------
    (token, payload)
        The opaque cookie value and the parsed payload (handy for
        logging the ``sid``).
    """
    iat = int(time.time()) if now is None else int(now)
    payload: SessionPayload = {
        "iat": iat,
        "exp": iat + SESSION_TTL_SECONDS,
        "nbf": iat,
        "sid": secrets.token_hex(16),
        "kfp": current_key_fingerprint(),
        "abs_exp": int(abs_exp) if abs_exp is not None else iat + SESSION_MAX_LIFETIME_SECONDS,
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _sign(payload_b64)
    return f"{payload_b64}.{sig}", payload


class SessionVerifyError(Exception):
    """Raised when a token fails any verification step.  ``reason`` is a
    short stable string used by the auth route to populate a structured
    401 body so the frontend can surface specific messages
    (``invalid_signature`` vs ``expired`` vs ``key_rotated``)."""

    def __init__(self, reason: str, message: Optional[str] = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


def verify_session_token(
    token: str,
    *,
    now: Optional[int] = None,
) -> SessionPayload:
    """Verify signature, expiry, key fingerprint.  Raises :class:`SessionVerifyError`
    with a stable ``reason`` on any failure; returns the parsed payload
    on success."""
    if not token or "." not in token:
        raise SessionVerifyError("malformed", "Token shape invalid")

    try:
        payload_b64, sig = token.rsplit(".", 1)
    except ValueError:
        raise SessionVerifyError("malformed", "Token shape invalid") from None

    expected_sig = _sign(payload_b64)
    if not hmac.compare_digest(expected_sig, sig):
        raise SessionVerifyError("invalid_signature", "Bad token signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        raise SessionVerifyError("malformed", "Token payload not JSON") from None

    # Defensive: an attacker can't forge fields (signed) but a bug in our own
    # code might mint short payloads. Bail loudly.
    for required in ("iat", "exp", "nbf", "sid", "kfp", "abs_exp"):
        if required not in payload:
            raise SessionVerifyError("malformed", f"Missing field: {required}")

    current = int(time.time()) if now is None else int(now)
    if current < int(payload["nbf"]):
        raise SessionVerifyError("not_yet_valid", "Token not yet valid")
    if current >= int(payload["exp"]):
        raise SessionVerifyError("expired", "Token expired")
    if current >= int(payload["abs_exp"]):
        raise SessionVerifyError("max_lifetime_exceeded", "Re-authentication required")
    if payload["kfp"] != current_key_fingerprint():
        # Most common cause: operator rotated JARVIS_API_KEY in Settings.
        raise SessionVerifyError("key_rotated", "Auth key rotated; please log in again")

    return payload  # type: ignore[return-value]


def refresh_session_token(token: str) -> tuple[str, SessionPayload]:
    """Mint a new token that inherits the original ``abs_exp`` ceiling.

    Caller must have already verified ``token`` (or it must be currently
    valid).  We re-verify here so a stale call site can't accidentally
    re-mint a dead token.
    """
    old = verify_session_token(token)
    return create_session_token(abs_exp=int(old["abs_exp"]))


def make_csrf_token() -> str:
    """CSRF tokens are independent from session tokens (double-submit
    cookie pattern).  128-bit random; readable by JS so it can echo
    the value back in the ``X-CSRF-Token`` header on mutations.
    """
    return secrets.token_urlsafe(24)
