"""Bounded browser-consent flows with push completion, never polling."""

from __future__ import annotations

import asyncio
import logging
import traceback
import secrets
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from services.plugins.package import PackageError
from services.plugins.remote_auth import RemoteTokenStore, provider, callback_origin
from services.plugins.remote_access import list_account_tools


@dataclass
class ConsentFlow:
    store: RemoteTokenStore
    id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    state: str | None = None
    callback: asyncio.Future | None = None
    url: asyncio.Future | None = None
    task: asyncio.Task | None = None
    connected: object = None


class RemoteConnections:
    def __init__(self):
        self.flows: dict[str, ConsentFlow] = {}

    async def start(
        self, store: RemoteTokenStore, redirect_uri: str, *, connected=None
    ):
        callback_origin(redirect_uri)
        if len(self.flows) >= 4 or any(
            f.store.candidate == store.candidate for f in self.flows.values()
        ):
            raise PackageError("Remote account connection already in progress")
        # Preflight encryption before sending any consent request.
        from core.secrets_crypto import encrypt

        encrypt("preflight")
        store.write("redirect", redirect_uri)
        loop = asyncio.get_running_loop()
        flow = ConsentFlow(
            store,
            callback=loop.create_future(),
            url=loop.create_future(),
            connected=connected,
        )
        self.flows[flow.id] = flow
        flow.task = asyncio.create_task(self._connect(flow))
        try:
            url = await asyncio.wait_for(asyncio.shield(flow.url), timeout=30)
            return {"flow_id": flow.id, "url": url}
        except BaseException:
            await self.cancel(flow.id)
            raise

    async def _connect(self, flow: ConsentFlow):
        async def redirect(url: str):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in {
                "auth.atlassian.com",
                "id.atlassian.com",
            }:
                raise PackageError("Unexpected remote authorization authority")
            state = parse_qs(parsed.query).get("state", [])
            if len(state) != 1 or not state[0]:
                raise PackageError("Remote authorization state missing")
            flow.state = state[0]
            if not flow.url.done():
                flow.url.set_result(url)

        async def callback():
            return await flow.callback

        try:
            async with asyncio.timeout(300):
                auth = provider(
                    flow.store, redirect_handler=redirect, callback_handler=callback
                )
                async with httpx.AsyncClient(
                    auth=auth, timeout=30, follow_redirects=False
                ) as client:
                    async with streamable_http_client(
                        flow.store.endpoint, http_client=client
                    ) as streams:
                        async with ClientSession(streams[0], streams[1]) as session:
                            await session.initialize()
                            tools = await list_account_tools(session, flow.store)
                            if not tools or not await flow.store.get_tokens():
                                raise PackageError(
                                    "Remote account did not authorize tool access"
                                )
            if flow.connected:
                await flow.connected()
            # Already-authorized storage does not necessarily redirect again.
            if not flow.url.done():
                flow.url.set_result(None)
            self._event(flow, "connected")
        except BaseException as exc:
            # Consent may succeed before immutable-package checks fail. Do not
            # retain a token that was never approved for this installed package.
            flow.store.clear()

            def diagnostic(error):
                return {
                    "type": type(error).__name__,
                    "frames": [
                        (f.filename.rsplit("/", 1)[-1], f.lineno, f.name)
                        for f in traceback.extract_tb(error.__traceback__)
                    ],
                    "causes": [diagnostic(e) for e in getattr(error, "exceptions", [])],
                }

            logging.getLogger(__name__).warning(
                "[plugins] Remote consent failed: %s", diagnostic(exc)
            )
            if not flow.url.done():
                flow.url.set_exception(PackageError("Remote account connection failed"))
            self._event(
                flow,
                "cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else "connection_failed",
            )
        finally:
            self.flows.pop(flow.id, None)

    def _event(self, flow, status):
        from services.activity_stream import activity_stream_manager

        activity_stream_manager.broadcast(
            {
                "event_type": "plugin_status",
                "agent_name": "",
                "data": {
                    "id": flow.store.candidate,
                    "remote_status": status,
                    "remote_server": flow.store.server,
                    "policy_configured": status == "connected",
                },
            }
        )

    async def complete(self, state: str, code: str | None, error: str | None):
        flow = next(
            (
                f
                for f in self.flows.values()
                if f.state and secrets.compare_digest(f.state, state)
            ),
            None,
        )
        if flow is None or flow.callback.done():
            raise PackageError("Unknown, expired, or already-used authorization state")
        if error or not code:
            flow.callback.set_exception(
                PackageError("Remote account consent was denied")
            )
        else:
            flow.callback.set_result((code, state))

    async def cancel(self, identity: str):
        flow = self.flows.get(identity)
        if flow:
            flow.task.cancel()
            await asyncio.gather(flow.task, return_exceptions=True)


remote_connections = RemoteConnections()
