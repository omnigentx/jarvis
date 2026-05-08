"""Google OAuth Web flow — Gmail + Calendar.

Replaces the legacy ``InstalledAppFlow.run_local_server()`` path which
required a browser on the same host as the MCP subprocess and therefore
fell over in Docker / headless deployments.

Design
------

* **Client id/secret/type** live in :class:`ConfigService` under
  ``oauth.google``. They're loaded on every request — no module-global
  singletons — so rotating them via the Settings UI takes effect immediately.
* ``client_type`` is ``"desktop"`` or ``"web"`` and determines which OAuth
  flow to run: Desktop-app clients use a fixed ``http://localhost`` loopback
  redirect (paste-URL UX), Web-application clients use a redirect URI the
  UI supplies (popup UX).
* **Access + refresh tokens** also live in ``config_service``. Each value is
  marked as a secret so they're Fernet-encrypted at rest under the master
  key.
* **Scopes** are stored as a space-separated string. The helper treats them
  as a set so accidentally duplicated scopes still match.
* The helper returns a ready-to-use :class:`google.oauth2.credentials.Credentials`
  so callers (gmail_server, calendar_server) don't need to know anything
  about our storage layout.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Literal, Optional
from urllib.parse import urlencode

import requests
from google.oauth2.credentials import Credentials

from services.config_service import config_service

logger = logging.getLogger(__name__)

OAUTH_CATEGORY = "oauth.google"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
DEFAULT_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_REVOKE_URI = "https://oauth2.googleapis.com/revoke"

# Fixed redirect URI for Desktop-app OAuth clients. Matches exactly what
# Google's "Desktop app" client type has on record (``redirect_uris:
# ["http://localhost"]``) so the token exchange later doesn't reject a
# port/path mismatch. After consent the browser lands on this URL with
# ``?code=...&state=...`` and shows "site can't be reached" (nothing
# listening on localhost:80) — the user then copies the full URL back into
# the UI.
DESKTOP_LOOPBACK_REDIRECT_URI = "http://localhost"

ClientType = Literal["desktop", "web"]

# Default scopes cover both Gmail and Calendar so a single consent screen
# is enough for the whole feature set.
DEFAULT_SCOPES: tuple[str, ...] = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar",
)

# Google Cloud APIs that MUST be enabled in the OAuth client's project for
# the declared scopes to work. Granting scopes at consent time is not enough
# — each API requires a separate per-project "Enable" click in the Cloud
# Console, otherwise runtime calls fail with HTTP 403 accessNotConfigured.
# Format: (display_name, api_service_name). api_service_name is the
# identifier used in ``console.developers.google.com`` URLs (different from
# the discovery name passed to ``googleapiclient.discovery.build``).
REQUIRED_APIS: tuple[tuple[str, str], ...] = (
    ("Gmail API", "gmail.googleapis.com"),
    ("Google Calendar API", "calendar-json.googleapis.com"),
)

@dataclass(frozen=True)
class GoogleOAuthClient:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class GoogleOAuthTokens:
    access_token: str
    refresh_token: Optional[str]
    expires_at: float  # unix seconds
    scopes: tuple[str, ...]
    token_uri: str


# ---- Storage helpers --------------------------------------------------------


def _get(key: str, *, default: Optional[str] = None) -> Optional[str]:
    return config_service.get(OAUTH_CATEGORY, key, default=default)


def load_client() -> Optional[GoogleOAuthClient]:
    """Return the OAuth client from the DB, or ``None`` if not saved."""
    cid = _get("client_id")
    secret = _get("client_secret")
    if cid and secret:
        return GoogleOAuthClient(client_id=cid, client_secret=secret)
    return None


def _project_number_from_client_id(client_id: Optional[str]) -> Optional[str]:
    """Extract the Cloud project number prefix from a Google OAuth client_id.

    Google issues client_ids formatted as
    ``<project_number>-<hash>.apps.googleusercontent.com`` — the leading
    digit run is the numeric Cloud project number. Returns ``None`` if the
    client_id doesn't match that shape (defensive — should always match
    for values Google actually handed out).
    """
    if not client_id or "-" not in client_id:
        return None
    prefix = client_id.split("-", 1)[0]
    return prefix if prefix.isdigit() else None


def project_number() -> Optional[str]:
    """Return the Cloud project number of the stored OAuth client, or None."""
    client = load_client()
    if client is None:
        return None
    return _project_number_from_client_id(client.client_id)


def api_enable_url(api_id: str, project_number: Optional[str] = None) -> str:
    """Return a Google Cloud Console deep-link that lets the user enable ``api_id``.

    The console accepts the URL without ``?project=…`` (it picks the user's
    most recent project) but pre-filling routes the user straight to the
    right project — the whole point of this flow.
    """
    base = f"https://console.developers.google.com/apis/api/{api_id}/overview"
    return f"{base}?project={project_number}" if project_number else base


def required_api_links() -> list[dict]:
    """Return pre-filled enable URLs for all Google APIs Jarvis needs.

    Used by the Settings UI to show a checklist of "go click Enable here"
    links right after OAuth succeeds, so the user doesn't hit a 403
    accessNotConfigured at runtime.
    """
    project = project_number()
    return [
        {
            "name": display_name,
            "api_id": api_id,
            "enable_url": api_enable_url(api_id, project),
        }
        for display_name, api_id in REQUIRED_APIS
    ]


def client_type() -> str:
    """Return the OAuth client type: ``"desktop"``, ``"web"``, or ``"none"``.

    Rows saved before ``client_type`` was introduced default to ``"web"``
    (the legacy custom-client behaviour) so existing deployments keep
    working without manual migration.
    """
    if load_client() is None:
        return "none"
    ct = _get("client_type") or "web"
    return ct if ct in ("desktop", "web") else "web"


def save_client(client_id: str, client_secret: str, client_type: ClientType) -> None:
    if not client_id or not client_secret:
        raise ValueError("client_id and client_secret are required")
    if client_type not in ("desktop", "web"):
        raise ValueError("client_type must be 'desktop' or 'web'")
    config_service.set_many(
        [
            (OAUTH_CATEGORY, "client_id", client_id, True),
            (OAUTH_CATEGORY, "client_secret", client_secret, True),
            (OAUTH_CATEGORY, "client_type", client_type, False),
        ],
        source="user",
    )


def clear_client() -> None:
    """Drop the stored client. Tokens are left alone — caller decides."""
    for key in ("client_id", "client_secret", "client_type"):
        config_service.set(OAUTH_CATEGORY, key, None, source="user")


GOOGLE_OAUTH_CLIENT_ID_ENV = "GOOGLE_OAUTH_CLIENT_ID"
GOOGLE_OAUTH_CLIENT_SECRET_ENV = "GOOGLE_OAUTH_CLIENT_SECRET"


def seed_client_from_env() -> bool:
    """Migrate legacy ``GOOGLE_OAUTH_CLIENT_ID/SECRET`` env vars into the DB.

    This is a one-shot upgrade step for deployments that predate the
    DB-first OAuth model. Called once from the backend bootstrap. Returns
    ``True`` when a seed row was written, ``False`` otherwise.

    Rules:
      * If the DB already has a ``client_id`` we never overwrite — the user's
        explicit Settings-UI choice always wins.
      * Both env vars must be present; a half-configured env (only id OR
        only secret) is treated as "not configured" so the user sees the
        empty-state form and can fill it in via the UI.
      * Seeded clients are saved with ``client_type="desktop"`` because that
        matches the prior README / docker-compose advice (loopback redirect,
        no redirect URI to register).
    """
    if load_client() is not None:
        return False
    env_id = os.environ.get(GOOGLE_OAUTH_CLIENT_ID_ENV)
    env_secret = os.environ.get(GOOGLE_OAUTH_CLIENT_SECRET_ENV)
    if not (env_id and env_id.strip()) or not (env_secret and env_secret.strip()):
        return False
    save_client(env_id.strip(), env_secret.strip(), "desktop")
    return True


def safe_seed_client_from_env(
    *, on_warn: Optional[Callable[[Exception], None]] = None,
) -> bool:
    """Bootstrap-safe wrapper around :func:`seed_client_from_env`.

    Mirrors :func:`services.runtime_config.reconcile_service_env`'s soft-fail
    policy: if the stored ``client_id``/``client_secret`` is encrypted under
    a rotated master key, ``config_service.get`` raises
    :class:`~core.secrets_crypto.DecryptError` — swallow it here so backend
    boot proceeds (the user re-sets via Settings when they next need Google
    OAuth) instead of crash-looping the whole container for one optional
    feature.

    All other exceptions propagate — those signal programming or infra bugs
    and should not be hidden.
    """
    from core.secrets_crypto import DecryptError
    try:
        return seed_client_from_env()
    except DecryptError as exc:
        if on_warn is not None:
            on_warn(exc)
        return False


def load_tokens() -> Optional[GoogleOAuthTokens]:
    access = _get("access_token")
    if not access:
        return None
    refresh = _get("refresh_token")
    expires_raw = _get("expires_at") or "0"
    try:
        expires_at = float(expires_raw)
    except ValueError:
        expires_at = 0.0
    scopes_raw = _get("scopes") or ""
    scopes = tuple(s for s in scopes_raw.split() if s)
    token_uri = _get("token_uri") or DEFAULT_TOKEN_URI
    return GoogleOAuthTokens(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        scopes=scopes,
        token_uri=token_uri,
    )


def save_tokens(tokens: GoogleOAuthTokens) -> None:
    """Write tokens atomically — via :meth:`set_many` so a failure doesn't
    leave the category half-populated."""
    items: list[tuple[str, str, Optional[str], bool]] = [
        (OAUTH_CATEGORY, "access_token", tokens.access_token, True),
        (OAUTH_CATEGORY, "expires_at", str(tokens.expires_at), False),
        (OAUTH_CATEGORY, "scopes", " ".join(tokens.scopes), False),
        (OAUTH_CATEGORY, "token_uri", tokens.token_uri, False),
    ]
    if tokens.refresh_token:
        items.append(
            (OAUTH_CATEGORY, "refresh_token", tokens.refresh_token, True)
        )
    config_service.set_many(items, source="user")


def clear_tokens() -> None:
    """Clear the stored tokens — used by the ``disconnect`` endpoint.

    Does *not* remove the client_id/client_secret so the user can reconnect
    without re-entering them.
    """
    for key in ("access_token", "refresh_token", "expires_at", "scopes", "token_uri"):
        config_service.set(OAUTH_CATEGORY, key, None, source="user")


# ---- OAuth flow -------------------------------------------------------------


def build_consent_url(
    *,
    redirect_uri: str,
    state: str,
    scopes: Iterable[str] = DEFAULT_SCOPES,
    prompt: str = "consent",
) -> str:
    """Return the Google consent URL for the stored client.

    ``prompt=consent`` forces Google to re-issue a refresh token on every
    auth — important because Google only returns one on the *first* consent
    by default, and missing it means we can never refresh silently later.
    """
    client = load_client()
    if client is None:
        raise RuntimeError(
            "Google OAuth client not configured — save client_id/client_secret via Settings."
        )
    params = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": prompt,
        "state": state,
    }
    return f"{DEFAULT_AUTH_URI}?{urlencode(params)}"


def exchange_code(*, code: str, redirect_uri: str) -> GoogleOAuthTokens:
    """Exchange the authorisation code for tokens and persist them.

    On success the refresh_token (if any) and access_token are written to
    the DB; on failure nothing is stored.
    """
    client = load_client()
    if client is None:
        raise RuntimeError("Google OAuth client not configured")

    resp = requests.post(
        DEFAULT_TOKEN_URI,
        data={
            "code": code,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=10,
    )
    if not resp.ok:
        logger.warning("OAuth code exchange failed: %s %s", resp.status_code, resp.text)
        raise RuntimeError(
            f"Google rejected the auth code ({resp.status_code}): {resp.text[:200]}"
        )
    body = resp.json()
    expires_in = int(body.get("expires_in", 3600))
    scope_string = body.get("scope") or " ".join(DEFAULT_SCOPES)
    tokens = GoogleOAuthTokens(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_at=time.time() + expires_in,
        scopes=tuple(s for s in scope_string.split() if s),
        token_uri=DEFAULT_TOKEN_URI,
    )
    save_tokens(tokens)
    return tokens


def _refresh(tokens: GoogleOAuthTokens) -> GoogleOAuthTokens:
    if not tokens.refresh_token:
        raise RuntimeError(
            "Refresh token missing — user must reconnect Google (prompt=consent)."
        )
    client = load_client()
    if client is None:
        raise RuntimeError("Google OAuth client not configured")

    resp = requests.post(
        tokens.token_uri,
        data={
            "refresh_token": tokens.refresh_token,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "grant_type": "refresh_token",
        },
        timeout=10,
    )
    if not resp.ok:
        logger.warning("OAuth refresh failed: %s %s", resp.status_code, resp.text)
        raise RuntimeError(
            f"Refresh failed ({resp.status_code}); user must reconnect Google."
        )
    body = resp.json()
    expires_in = int(body.get("expires_in", 3600))
    scope_string = body.get("scope") or " ".join(tokens.scopes)
    refreshed = GoogleOAuthTokens(
        access_token=body["access_token"],
        # Google only re-issues a refresh_token occasionally; keep the old
        # one if the response omits it.
        refresh_token=body.get("refresh_token") or tokens.refresh_token,
        expires_at=time.time() + expires_in,
        scopes=tuple(s for s in scope_string.split() if s),
        token_uri=tokens.token_uri,
    )
    save_tokens(refreshed)
    return refreshed


def get_credentials(*, skew: int = 60) -> Optional[Credentials]:
    """Return a refreshed :class:`Credentials` or ``None`` if not connected.

    ``skew`` buys us ``n`` seconds of safety margin — we refresh proactively
    before the token actually expires to avoid races where the token goes
    stale between this check and the eventual API call.
    """
    tokens = load_tokens()
    if tokens is None:
        return None
    if tokens.expires_at - skew <= time.time():
        try:
            tokens = _refresh(tokens)
        except RuntimeError as exc:
            logger.warning("Google OAuth refresh: %s", exc)
            return None

    client = load_client()
    if client is None:
        return None
    return Credentials(
        token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        token_uri=tokens.token_uri,
        client_id=client.client_id,
        client_secret=client.client_secret,
        scopes=list(tokens.scopes),
    )


def revoke() -> None:
    """Best-effort remote revocation followed by local clear."""
    tokens = load_tokens()
    if tokens and tokens.access_token:
        try:
            requests.post(
                DEFAULT_REVOKE_URI,
                params={"token": tokens.refresh_token or tokens.access_token},
                timeout=5,
            )
        except Exception as exc:
            logger.info("Google revoke call failed (continuing with local clear): %s", exc)
    clear_tokens()
