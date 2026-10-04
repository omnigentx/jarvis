"""Encrypted SDK OAuth storage, shared by UI consent and remote workers."""

from __future__ import annotations

import fcntl
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import httpx
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from sqlalchemy import text
from sqlalchemy.engine import Engine

from core.secrets_crypto import decrypt, encrypt
from services.plugins.compatibility import ROVO_ENDPOINTS
from services.plugins.package import PackageError

REMOTE_POLICY = "remote-oauth"


def approved_endpoint(server: dict) -> str:
    """Only the reviewed native Rovo adapter is currently network-enabled."""
    url = server.get("url")
    if (
        server.get("transport", server.get("type")) != "http"
        or url not in ROVO_ENDPOINTS
        or set(server) - {"type", "transport", "url"}
    ):
        raise PackageError("Remote MCP endpoint needs an approved native adapter")
    return url


class RemoteTokenStore(TokenStorage):
    def __init__(self, engine: Engine, candidate: str, server: str, endpoint: str):
        self.engine, self.candidate, self.server, self.endpoint = (
            engine,
            candidate,
            server,
            endpoint,
        )
        with engine.begin() as db:
            db.execute(
                text("""CREATE TABLE IF NOT EXISTS plugin_remote_auth (
                candidate TEXT NOT NULL, server TEXT NOT NULL, endpoint TEXT NOT NULL,
                tokens TEXT, client TEXT, redirect TEXT, updated REAL,
                PRIMARY KEY(candidate,server)
            )""")
            )
            db.execute(
                text("""INSERT OR IGNORE INTO plugin_remote_auth
                (candidate,server,endpoint) VALUES (:c,:s,:e)"""),
                self.params,
            )
        row = self.read()
        if row["endpoint"] != endpoint:
            raise PackageError("Remote authorization endpoint changed")

    @property
    def params(self):
        return {"c": self.candidate, "s": self.server, "e": self.endpoint}

    def read(self):
        with self.engine.connect() as db:
            row = (
                db.execute(
                    text(
                        "SELECT * FROM plugin_remote_auth WHERE candidate=:c AND server=:s"
                    ),
                    self.params,
                )
                .mappings()
                .first()
            )
        return dict(row) if row else {}

    def write(self, column: str, value: str):
        if column not in {"tokens", "client", "redirect"}:
            raise ValueError("Invalid auth field")
        encrypted = encrypt(value) if column != "redirect" else value
        with self.engine.begin() as db:
            db.execute(
                text(
                    f"UPDATE plugin_remote_auth SET {column}=:v, updated=:t WHERE candidate=:c AND server=:s AND endpoint=:e"
                ),
                {**self.params, "v": encrypted, "t": time.time()},
            )

    async def get_tokens(self):
        value = self.read().get("tokens")
        return OAuthToken.model_validate_json(decrypt(value)) if value else None

    async def set_tokens(self, tokens):
        self.write("tokens", tokens.model_dump_json())

    async def get_client_info(self):
        value = self.read().get("client")
        return (
            OAuthClientInformationFull.model_validate_json(decrypt(value))
            if value
            else None
        )

    async def set_client_info(self, client_info):
        self.write("client", client_info.model_dump_json())

    def clear(self):
        with self.engine.begin() as db:
            db.execute(
                text(
                    "UPDATE plugin_remote_auth SET tokens=NULL,client=NULL,redirect=NULL WHERE candidate=:c AND server=:s"
                ),
                self.params,
            )

    @asynccontextmanager
    async def serialized(self):
        """Serialize rotating refresh tokens across backend and team processes."""
        import hashlib

        name = hashlib.sha256(
            (self.candidate + "\0" + self.server).encode()
        ).hexdigest()
        path = Path(self.engine.url.database).parent / "plugin-auth-locks"
        path.mkdir(mode=0o700, exist_ok=True)
        with (path / (name[:2] + ".lock")).open("a") as lock:
            # anyio defers cancellation until the thread has actually acquired
            # the lock, so cancellation cannot orphan an eventual flock.
            await anyio.to_thread.run_sync(fcntl.flock, lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class SerializedOAuthProvider(httpx.Auth):
    requires_response_body = True

    def __init__(self, store, arguments):
        self.store, self.arguments = store, arguments

    async def async_auth_flow(self, request):
        async with self.store.serialized():
            auth = OAuthClientProvider(**self.arguments)
            flow = auth.async_auth_flow(request)
            try:
                item = await flow.__anext__()
                while True:
                    response = yield item
                    try:
                        item = await flow.asend(response)
                    except StopAsyncIteration:
                        break
            finally:
                await flow.aclose()


def provider(store: RemoteTokenStore, *, redirect_handler=None, callback_handler=None):
    async def unavailable(*_args):
        raise PackageError(
            "Remote account authorization expired; reconnect in Settings"
        )

    redirect = store.read().get("redirect")
    if not redirect:
        raise PackageError("Connect the remote account in Settings first")
    return SerializedOAuthProvider(
        store,
        dict(
            server_url=store.endpoint,
            client_metadata=OAuthClientMetadata(
                client_name="Jarvis",
                redirect_uris=[redirect],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            ),
            storage=store,
            redirect_handler=redirect_handler or unavailable,
            callback_handler=callback_handler or unavailable,
            timeout=300,
        ),
    )


def callback_origin(value: str) -> str:
    """External deployments must declare their public consent origin."""
    import os

    u = urlsplit(value)
    configured = os.environ.get("JARVIS_PUBLIC_URL", "").rstrip("/")
    origin = f"{u.scheme}://{u.netloc}"
    local = u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1"}
    if (
        u.username
        or u.password
        or u.query
        or u.fragment
        or u.path != "/api/plugins/oauth/callback"
        or not (local or (configured and origin == configured and u.scheme == "https"))
    ):
        raise PackageError("OAuth callback must use the configured Jarvis public URL")
    return value
