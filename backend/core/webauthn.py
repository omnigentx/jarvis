"""WebAuthn / FIDO2 passkey helpers.

Thin wrapper around the `webauthn` (Duo Security) library that handles
the three Jarvis-specific concerns the library deliberately leaves to
the caller:

1. **RP ID and origin derivation from the live request.** Jarvis is
   self-hosted — each user runs their own instance at *their* domain
   (``localhost``, LAN IP with HTTPS, tailscale hostname, or a custom
   reverse-proxy domain). We derive RP ID and origin from the inbound
   ``Host`` / ``X-Forwarded-Host`` headers so a single binary works
   across all of these without configuration.

2. **Challenge storage between begin / finish.** WebAuthn ceremonies
   are two-call: ``/begin`` generates a challenge that the
   authenticator signs, and ``/finish`` verifies the signature. The
   challenge MUST be remembered server-side across the two calls (the
   client cannot be trusted to echo it intact). We use an in-process
   dict keyed by a random ``ceremony_id`` with a 5-minute TTL. This
   assumes a single backend worker — fine for Jarvis self-hosted, but
   if you ever fan out to multiple uvicorn workers behind a load
   balancer, swap this for Redis/DB-backed storage.

3. **Single-user identity binding.** Every credential is bound to the
   seeded ``DEFAULT_USER_ID`` row (``username='owner'``). The schema
   already supports multi-user via the FK, but routes here assume
   single-user — when multi-user lands, ``user_id`` becomes a route
   parameter rather than a constant.

Recovery: passkeys cannot be exported off the device. If the user
loses every passkey, the documented recovery path is to read
``JARVIS_API_KEY`` from ``.env`` and log in via the legacy API-key
flow, then re-register a passkey from settings.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Request
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

logger = logging.getLogger(__name__)


RP_NAME = "Jarvis"

# Ceremonies (register or authenticate) have to complete inside this
# window. Matches the WebAuthn default ``timeout`` field of 60s with
# slack for user inspection of the platform UI.
_CEREMONY_TTL_SECONDS = 5 * 60


# ---- Request-derived RP ID and origin --------------------------------------


def _request_host(request: Request) -> str:
    """Return the public host (with port) the user actually hit.

    Honors ``X-Forwarded-Host`` so a reverse proxy (Caddy, nginx,
    Tailscale Funnel) still gives the right RP ID. The Host header
    falls back to ``request.url.netloc`` if neither is present.
    """
    forwarded = request.headers.get("x-forwarded-host", "")
    if forwarded:
        # X-Forwarded-Host may carry multiple comma-separated entries
        # (proxy chain); the leftmost is the original client target.
        return forwarded.split(",")[0].strip()
    host = request.headers.get("host", "")
    return host or request.url.netloc


def _request_scheme(request: Request) -> str:
    if request.url.scheme == "https":
        return "https"
    forwarded = request.headers.get("x-forwarded-proto", "")
    if "https" in forwarded.lower():
        return "https"
    return "http"


def rp_id_from_request(request: Request) -> str:
    """RP ID = the *hostname only* (no port). FIDO2 spec requirement.

    Examples:
        ``localhost:3001`` → ``localhost``
        ``jarvis.alice.com`` → ``jarvis.alice.com``
        ``192.168.1.50:8001`` → ``192.168.1.50``  (won't pass browser
            WebAuthn check unless served via HTTPS; documented gotcha)
    """
    host = _request_host(request)
    # Strip port if present. IPv6 hosts arrive bracketed as ``[::1]:8000``.
    if host.startswith("["):
        # IPv6: ``[addr]:port`` → ``[addr]`` → ``addr``
        bracket_end = host.find("]")
        if bracket_end > 0:
            return host[1:bracket_end]
        return host
    if ":" in host:
        return host.rsplit(":", 1)[0]
    return host


# Hosts the WebAuthn spec allows to run over plaintext http (the secure-
# context dev exception). Every other host is necessarily https in a browser.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_loopback_host(rp_id: str) -> bool:
    """True for the hosts a browser will run WebAuthn on over plain http."""
    return rp_id in _LOOPBACK_HOSTS


def origin_from_request(request: Request) -> str:
    """Origin = scheme://host, used to verify the ``clientDataJSON.origin``
    field the authenticator signs.

    The scheme is INFERRED from the host, not trusted from the proxy.
    WebAuthn only runs in a secure context, so the browser-signed origin is
    ``https`` for every host except loopback (localhost/127.0.0.1/::1 — the
    spec's one plaintext dev exception). A reverse-proxy chain that
    terminates TLS upstream and speaks plain http internally (Cloudflare
    tunnel → nginx → app) reports scheme=http / X-Forwarded-Proto=http;
    trusting that builds ``http://<domain>`` and fails verification with
    InvalidRegistrationResponse. Host-based inference is proxy-independent,
    so passkeys work on any domain with zero proxy config — the user never
    configures an RP ID, origin, or X-Forwarded-* header.
    """
    host = _request_host(request)
    if _is_loopback_host(rp_id_from_request(request)):
        return f"{_request_scheme(request)}://{host}"
    return f"https://{host}"


# ---- Ceremony storage (in-process, TTL-bounded) ----------------------------


@dataclass
class _Ceremony:
    challenge: bytes
    rp_id: str
    expires_at: float
    # For register: bound to a specific authenticated user so a
    # man-in-the-middle can't substitute the user_id between begin and
    # finish. None for authenticate (we don't know the user yet).
    user_id: Optional[str] = None
    kind: str = "register"  # "register" | "authenticate"


_ceremonies: dict[str, _Ceremony] = {}


def _gc_ceremonies(now: Optional[float] = None) -> None:
    """Drop expired ceremonies. Cheap O(n) scan; n stays small because
    each ceremony lasts 5 min and ceremonies-per-second is human-paced."""
    now = now if now is not None else time.time()
    expired = [cid for cid, c in _ceremonies.items() if c.expires_at <= now]
    for cid in expired:
        _ceremonies.pop(cid, None)


def _new_ceremony_id() -> str:
    return secrets.token_urlsafe(24)


def store_ceremony(
    *,
    challenge: bytes,
    rp_id: str,
    user_id: Optional[str],
    kind: str,
) -> str:
    """Store challenge + return ceremony_id the client will echo back."""
    _gc_ceremonies()
    cid = _new_ceremony_id()
    _ceremonies[cid] = _Ceremony(
        challenge=challenge,
        rp_id=rp_id,
        user_id=user_id,
        kind=kind,
        expires_at=time.time() + _CEREMONY_TTL_SECONDS,
    )
    return cid


def pop_ceremony(ceremony_id: str, *, kind: str) -> Optional[_Ceremony]:
    """Single-use lookup: removes the ceremony before returning. Returns
    None if the id is unknown, expired, or for the wrong ceremony kind."""
    _gc_ceremonies()
    entry = _ceremonies.pop(ceremony_id, None)
    if entry is None:
        return None
    if entry.kind != kind:
        # Don't put it back — caller mis-routed; treat as a hostile
        # cross-flow attempt.
        return None
    return entry


def _ceremony_count() -> int:
    """Test hook: how many live ceremonies are in flight."""
    _gc_ceremonies()
    return len(_ceremonies)


def _clear_ceremonies() -> None:
    """Test hook."""
    _ceremonies.clear()


# ---- Ceremony helpers ------------------------------------------------------


def build_registration_options(
    *,
    request: Request,
    user_id: str,
    username: str,
    existing_credential_ids: list[str],
) -> tuple[str, dict]:
    """Return ``(ceremony_id, options_dict)`` for ``/register/begin``.

    ``options_dict`` is JSON-serializable (already converted from the
    library's structs), ready to ship as the response body. The client
    feeds it into ``navigator.credentials.create()``.

    ``existing_credential_ids`` are base64url strings already stored
    for this user *on this RP*; we pass them as ``excludeCredentials``
    so the browser refuses to re-register an authenticator that already
    has a passkey for this site.
    """
    rp_id = rp_id_from_request(request)
    exclude = [
        PublicKeyCredentialDescriptor(id=_b64url_decode(cid))
        for cid in existing_credential_ids
    ]
    opts = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_id=user_id.encode("utf-8"),
        user_name=username,
        user_display_name=username,
        # Require a resident (discoverable) credential so the
        # authenticator can offer this passkey on its own without the
        # server having to send allowCredentials. This is what makes
        # "Sign in with passkey" possible without first asking for a
        # username.
        #
        # ``user_verification=REQUIRED`` is intentional: this is the
        # SPA's primary credential (not a 2nd-factor), so the assertion
        # MUST prove user presence + verification (Touch ID, Face ID,
        # Windows Hello PIN, FIDO2 device PIN). PREFERRED would let an
        # authenticator that *can* skip UV authenticate without it,
        # making a stolen YubiKey without PIN sufficient for sign-in.
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=exclude,
    )
    cid = store_ceremony(
        challenge=opts.challenge,
        rp_id=rp_id,
        user_id=user_id,
        kind="register",
    )
    return cid, json.loads(options_to_json(opts))


def verify_registration(
    *,
    request: Request,
    ceremony_id: str,
    credential: dict,
    expected_user_id: str,
):
    """Verify the attestation response from ``/register/finish``.

    Returns the library's ``VerifiedRegistration`` with
    ``credential_id``, ``credential_public_key``, ``sign_count``, etc.
    Raises ``ValueError`` if the ceremony is unknown, expired, bound to
    a different user, or the attestation fails.
    """
    entry = pop_ceremony(ceremony_id, kind="register")
    if entry is None:
        raise ValueError("ceremony_unknown_or_expired")
    if entry.user_id != expected_user_id:
        # Belt-and-braces: in single-user mode this can't really happen
        # because there is only one user, but we still bind so the
        # invariant survives a future multi-user migration.
        raise ValueError("ceremony_user_mismatch")
    rp_id = rp_id_from_request(request)
    if entry.rp_id != rp_id:
        raise ValueError("ceremony_rp_mismatch")
    return verify_registration_response(
        credential=credential,
        expected_challenge=entry.challenge,
        expected_rp_id=rp_id,
        expected_origin=origin_from_request(request),
        # Match the REQUIRED policy in build_registration_options — UV
        # is mandatory for primary-credential passkeys. See the
        # authenticator_selection comment above for the threat model.
        require_user_verification=True,
    )


def build_authentication_options(
    *,
    request: Request,
    allow_credential_ids: list[str],
) -> tuple[str, dict]:
    """Return ``(ceremony_id, options_dict)`` for
    ``/authenticate/begin``.

    ``allow_credential_ids`` is the list of base64url credential ids
    registered for this RP. We pass them as ``allowCredentials`` so the
    browser knows which authenticators to offer; an empty list means
    "any discoverable credential" (works because we registered with
    resident_key=REQUIRED)."""
    rp_id = rp_id_from_request(request)
    allow = [
        PublicKeyCredentialDescriptor(id=_b64url_decode(cid))
        for cid in allow_credential_ids
    ]
    opts = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow if allow else None,
        # REQUIRED matches the register policy — see
        # ``build_registration_options`` for the threat model. UV is
        # what makes the passkey a real credential and not just a
        # possession factor.
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    cid = store_ceremony(
        challenge=opts.challenge,
        rp_id=rp_id,
        user_id=None,
        kind="authenticate",
    )
    return cid, json.loads(options_to_json(opts))


def verify_authentication(
    *,
    request: Request,
    ceremony_id: str,
    credential: dict,
    credential_public_key: bytes,
    credential_current_sign_count: int,
):
    """Verify the assertion response from ``/authenticate/finish``.

    Returns the library's ``VerifiedAuthentication``
    (``new_sign_count`` is the most useful field — caller MUST persist
    it to detect cloned credentials). Raises ``ValueError`` on any
    mismatch.
    """
    entry = pop_ceremony(ceremony_id, kind="authenticate")
    if entry is None:
        raise ValueError("ceremony_unknown_or_expired")
    rp_id = rp_id_from_request(request)
    if entry.rp_id != rp_id:
        raise ValueError("ceremony_rp_mismatch")
    return verify_authentication_response(
        credential=credential,
        expected_challenge=entry.challenge,
        expected_rp_id=rp_id,
        expected_origin=origin_from_request(request),
        credential_public_key=credential_public_key,
        credential_current_sign_count=credential_current_sign_count,
        # Match the REQUIRED policy in build_authentication_options.
        # The library raises ``InvalidAuthenticationResponse`` if the
        # assertion's UV flag is unset, which the route surfaces as
        # 401 — so a YubiKey-without-PIN attempt fails closed.
        require_user_verification=True,
    )


# ---- base64url helpers -----------------------------------------------------


def _b64url_decode(s: str) -> bytes:
    """Decode a base64url string (no padding) to bytes."""
    import base64
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def b64url_encode(b: bytes) -> str:
    """Encode bytes to a base64url string (no padding)."""
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")
