"""Host-owned RFC 6749 refresh for persisted Rovo credentials.

The SDK retains ownership of consent/PKCE and MCP. It does not restore token
expiry or discovered token endpoints from TokenStorage on recreation. This
HTTPX auth extension uses persisted public protocol data, never SDK context.
"""

import time
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp.shared.auth import OAuthToken

from services.plugins.package import PackageError


def allowed_oauth_url(value: str):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname
        not in {"mcp.atlassian.com", "auth.atlassian.com", "id.atlassian.com"}
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise PackageError("Unexpected remote authorization authority")


def observe_oauth(store, request, response):
    """Capture the issuer's token endpoint and SDK-selected resource binding."""
    if (
        "/.well-known/oauth-authorization-server" in request.url.path
        and response.status_code == 200
    ):
        try:
            endpoint = response.json().get("token_endpoint")
        except ValueError:
            return  # SDK handles invalid discovery without logging payloads.
        if endpoint:
            allowed_oauth_url(endpoint)
            store.oauth_settings["token_endpoint"] = endpoint
    if request.method == "POST" and str(request.url) == store.oauth_settings.get(
        "token_endpoint"
    ):
        resource = parse_qs(request.content.decode()).get("resource", [])
        if resource:
            allowed_oauth_url(resource[0])
            if urlsplit(resource[0]).hostname != "mcp.atlassian.com":
                raise PackageError("Unexpected remote OAuth resource")
            store.oauth_settings["resource"] = resource[0]


async def refresh_request(store):
    data = store.token_data()
    expiry = data.get("_jarvis_expires_at")
    if expiry is None or time.time() < expiry:
        return None
    settings = data.get("_jarvis_oauth", {})
    client = await store.get_client_info()
    if (
        not data.get("refresh_token")
        or not settings.get("token_endpoint")
        or not client
    ):
        store.invalidate_tokens()
        raise PackageError(
            "Remote account authorization expired; reconnect in Settings"
        )
    endpoint = settings["token_endpoint"]
    allowed_oauth_url(endpoint)
    if client.token_endpoint_auth_method not in {None, "none"}:
        raise PackageError(
            "Remote refresh authentication method is unsupported; reconnect in Settings"
        )
    form = {
        "grant_type": "refresh_token",
        "refresh_token": data["refresh_token"],
        "client_id": client.client_id,
    }
    if settings.get("resource"):
        allowed_oauth_url(settings["resource"])
        form["resource"] = settings["resource"]
    store.oauth_settings = settings
    return httpx.Request("POST", endpoint, data=form)


async def complete_refresh(store, response):
    if response.status_code in {400, 401, 403}:
        store.invalidate_tokens()
        raise PackageError(
            "Remote account authorization expired; reconnect in Settings"
        )
    if response.status_code != 200:
        raise PackageError(
            "Remote account refresh temporarily unavailable; try again later"
        )
    try:
        token = OAuthToken.model_validate_json(await response.aread())
    except ValueError:
        raise PackageError(
            "Remote account refresh returned an invalid response"
        ) from None
    # RFC 6749 section 6: an omitted refresh token retains the current one.
    if token.refresh_token is None:
        token.refresh_token = store.token_data().get("refresh_token")
    await store.set_tokens(token)
