"""Tests for routes/system.py — restart endpoint."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth as core_auth


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", "unit-test-master-key-xyz")
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "unit-test-master-key-xyz")
    yield


@pytest.fixture()
def client():
    from routes.system import router as system_router

    app = FastAPI()
    app.include_router(system_router)
    return TestClient(app)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {core_auth.JARVIS_API_KEY}"}


class TestRestart:
    def test_requires_auth(self, client):
        assert client.post("/api/system/restart").status_code == 401

    def test_returns_pid_and_schedules_signal(self, client, monkeypatch):
        """Endpoint must respond immediately and schedule a SIGTERM in the background.

        We intercept ``os.kill`` so the test process is NOT actually killed —
        instead we record that the call was attempted.
        """
        import routes.system as system_routes

        kill_calls: list[tuple[int, int]] = []

        def fake_kill(pid, sig):
            kill_calls.append((pid, sig))

        monkeypatch.setattr(system_routes.os, "kill", fake_kill)
        # Zero out the delay so we don't need to sleep in the test.
        original = system_routes._trigger_exit

        async def fast_exit(delay):  # noqa: ARG001
            await original(0)

        monkeypatch.setattr(system_routes, "_trigger_exit", fast_exit)

        resp = client.post("/api/system/restart", headers=_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["restarting"] is True
        assert isinstance(body["pid"], int)

        # Give the background task a chance to fire.
        import time

        for _ in range(20):
            if kill_calls:
                break
            time.sleep(0.05)

        assert len(kill_calls) == 1
        assert kill_calls[0][0] == body["pid"]

    def test_falls_back_to_os_exit_on_kill_failure(self, client, monkeypatch):
        """If ``os.kill`` raises we should escalate to ``os._exit``."""
        import routes.system as system_routes

        def boom(*_a, **_k):
            raise OSError("nope")

        exit_calls: list[int] = []

        def fake_exit(code):
            exit_calls.append(code)

        monkeypatch.setattr(system_routes.os, "kill", boom)
        monkeypatch.setattr(system_routes.os, "_exit", fake_exit)

        async def run():
            await system_routes._trigger_exit(0)

        asyncio.run(run())
        assert exit_calls == [0]
