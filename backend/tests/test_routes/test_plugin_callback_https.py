"""Real TLS acceptance of the production callback handler, with synthetic consent.

No vendor account, code or browser TLS exception is used. State/callback logic
is real; the authorization-code exchange remains covered separately.
"""

import asyncio
import socket
import ssl
import subprocess
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import uvicorn
from fastapi import FastAPI

from routes import plugin_remote
from services.plugins.remote_auth import callback_origin
from services.plugins.remote_connection import ConsentFlow, RemoteConnections


def test_https_callback_validated_certificate_and_one_use(tmp_path, monkeypatch):
    key, certificate = tmp_path / "tls.key", tmp_path / "tls.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
    )
    manager = RemoteConnections()
    monkeypatch.setattr(plugin_remote, "remote_connections", manager)
    accepted = []

    @asynccontextmanager
    async def lifespan(_app):
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda done: accepted.append(done.result()))
        flow = ConsentFlow(
            SimpleNamespace(candidate="synthetic-consent"),
            state="test-only-state",
            callback=future,
        )
        manager.flows[flow.id] = flow
        yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(plugin_remote.router)
    ready = threading.Event()

    class TLSCallbackServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"https://127.0.0.1:{listener.getsockname()[1]}"
        monkeypatch.setenv("JARVIS_PUBLIC_URL", origin)
        callback = callback_origin(origin + "/api/plugins/oauth/callback")
        server = TLSCallbackServer(
            uvicorn.Config(
                app,
                ssl_keyfile=str(key),
                ssl_certfile=str(certificate),
                access_log=False,
                log_level="warning",
            )
        )
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        thread.start()
        try:
            assert ready.wait(10), "TLS callback server failed to start"
            # Certificate and hostname validation enabled; no verify=False.
            with httpx.Client(
                verify=ssl.create_default_context(cafile=str(certificate)),
                timeout=10,
                trust_env=False,
            ) as client:
                wrong = client.get(
                    callback, params={"state": "wrong-state", "code": "test-only-code"}
                )
                assert wrong.status_code == 400
                assert not accepted
                ok = client.get(
                    callback,
                    params={"state": "test-only-state", "code": "test-only-code"},
                )
                assert ok.status_code == 200
                assert ok.headers["cache-control"] == "no-store"
                assert ok.headers["referrer-policy"] == "no-referrer"
                assert "test-only-code" not in ok.text
                replay = client.get(
                    callback,
                    params={"state": "test-only-state", "code": "test-only-code"},
                )
                assert replay.status_code == 400
                assert accepted == [("test-only-code", "test-only-state")]
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "TLS callback server did not shut down"
