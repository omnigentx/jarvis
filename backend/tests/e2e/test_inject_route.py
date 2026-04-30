"""E2E tests for POST /api/agents/{agent_name}/inject (routes/inject.py).

Covers the three decision branches inside ``inject_prompt``:

  Path A — MessageBus   : spawn record alive (running/pending/paused)
  Path B — Resume       : spawn record dead but has original_config
  Path C — generate()   : agent is a static member of ``fast.agents``

Plus the error branches:
  * 404 when neither registry nor fast.agents knows the agent
  * 409 when registry has the agent idle but no saved config
  * 401 when JARVIS_API_KEY is configured and the caller sends a wrong one

Every external collaborator is mocked at the exact import path used by
``routes.inject`` at call-time.  No subprocess, no real LLM, no real
MessageBus filesystem — the test asserts *routing decisions*, not the
internals of the three paths.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


# ─────────────────────────────────────────────────────────────
# Auto-applied fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _bypass_setup_gate(monkeypatch):
    """The setup-gate middleware rejects /api/* with 503 until the setup wizard
    is complete.  These tests don't care about the wizard — pretend it's done.
    """
    from middleware import setup_gate

    monkeypatch.setattr(setup_gate, "is_setup_complete", lambda: True)


@pytest.fixture(autouse=True)
def _isolate_server_env_mutation():
    """server.py has a top-level ``os.environ.setdefault("SPAWN_REGISTRY_DB", ...)``
    that leaks into subsequent tests once imported.  Snapshot env before the
    test and restore anything server.py may have added.
    """
    before = {k: os.environ[k] for k in ("SPAWN_REGISTRY_DB", "SPAWN_PROJECT_DIR")
              if k in os.environ}
    yield
    for k in ("SPAWN_REGISTRY_DB", "SPAWN_PROJECT_DIR"):
        if k in before:
            os.environ[k] = before[k]
        else:
            os.environ.pop(k, None)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def _make_client() -> httpx.AsyncClient:
    """ASGI client for the real FastAPI app with a Bearer header set.

    Auth is effectively disabled unless we monkeypatch
    ``core.auth.JARVIS_API_KEY`` — conftest does not configure auth.
    """
    from server import app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
    )


# ─────────────────────────────────────────────────────────────
# Path A: MessageBus (process alive)
# ─────────────────────────────────────────────────────────────


async def test_inject_running_agent_queues_via_messagebus(monkeypatch, tmp_path):
    """Running agent → MessageBus send, path=message_bus, status=queued."""
    import services.shared_state as state
    import fast_agent.spawn.message_bus as mb_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    fake_record = {
        "agent_name": "TestAgent",
        "status": "running",
        "workspace": str(workspace),
        "started_at": 1.0,
    }
    fake_registry = MagicMock()
    fake_registry.find_by_name = MagicMock(return_value=[fake_record])
    monkeypatch.setattr(state, "registry_db", fake_registry)

    # Capture MessageBus.send() call without touching the real class
    bus_instance = MagicMock()
    bus_instance.send = MagicMock()
    bus_ctor = MagicMock(return_value=bus_instance)
    monkeypatch.setattr(mb_mod, "MessageBus", bus_ctor)

    async with _make_client() as client:
        resp = await client.post(
            "/api/agents/TestAgent/inject",
            json={"message": "hi", "priority": "normal"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["path"] == "message_bus"
    assert body["agent_name"] == "TestAgent"
    assert body["response"] is None

    fake_registry.find_by_name.assert_called_once_with("TestAgent")
    bus_ctor.assert_called_once()
    bus_instance.send.assert_called_once()
    send_kwargs = bus_instance.send.call_args.kwargs
    assert send_kwargs.get("from_name") == "Dashboard"
    assert send_kwargs.get("to_name") == "TestAgent"
    assert send_kwargs.get("content") == "hi"


# ─────────────────────────────────────────────────────────────
# Path B: Resume (process dead but resumable)
# ─────────────────────────────────────────────────────────────


async def test_inject_idle_agent_with_config_triggers_resume(monkeypatch):
    """Idle agent with original_config → resume_with_inject, path=resume_with_context."""
    import services.shared_state as state
    import services.inject_resume as inject_resume_mod

    fake_record = {
        "agent_name": "DeadAgent",
        "status": "idle",
        "started_at": 2.0,
        "original_config": {
            "instruction": "You are a helper.",
            "servers": [],
            "model": "gpt-4o-mini",
            "role": "helper",
            "team_name": "",
            "project_dir": ".",
        },
    }
    fake_registry = MagicMock()
    fake_registry.find_by_name = MagicMock(return_value=[fake_record])
    monkeypatch.setattr(state, "registry_db", fake_registry)
    monkeypatch.setattr(state, "spawn_bridge", None)

    # Mock the resume helper — the route imports it lazily via
    # `from services.inject_resume import resume_with_inject`
    resume_mock = AsyncMock(return_value={
        "status": "resumed",
        "run_id": "run-resume-123",
        "agent_name": "DeadAgent",
    })
    monkeypatch.setattr(inject_resume_mod, "resume_with_inject", resume_mock)

    async with _make_client() as client:
        resp = await client.post(
            "/api/agents/DeadAgent/inject",
            json={"message": "wake up", "priority": "normal"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "resumed"
    assert body["path"] == "resume_with_context"
    assert body["agent_name"] == "DeadAgent"
    assert "run-resume-123" in (body["response"] or "")

    resume_mock.assert_awaited_once()
    call_kwargs = resume_mock.await_args.kwargs
    assert call_kwargs["agent_name"] == "DeadAgent"
    assert call_kwargs["inject_message"] == "wake up"
    assert call_kwargs["spawn_record"] is fake_record


# ─────────────────────────────────────────────────────────────
# Path C: Static agent via generate()
# ─────────────────────────────────────────────────────────────


async def test_inject_static_agent_generates_inline(monkeypatch):
    """Static agent (no spawn record, present in fast.agents) → generate()."""
    import services.shared_state as state
    import routes.inject as inject_mod

    # Registry returns no records → route falls through to Path C
    fake_registry = MagicMock()
    fake_registry.find_by_name = MagicMock(return_value=[])
    monkeypatch.setattr(state, "registry_db", fake_registry)

    # Fake agent with async generate() that returns a result exposing .last_text()
    result = MagicMock()
    result.last_text = MagicMock(return_value="static reply")

    fake_agent = MagicMock()
    fake_agent.generate = AsyncMock(return_value=result)
    fake_agent.tool_runner_hooks = None

    # agent_app must satisfy both:
    #   * getattr(agent_app, agent_name) → fake_agent
    #   * agent_app._agents.items()       → dict iteration for hook attach/restore
    agent_app = MagicMock()
    agent_app._agents = {"StaticAgent": fake_agent}
    # Ensure getattr(agent_app, "StaticAgent") returns our fake
    agent_app.StaticAgent = fake_agent
    monkeypatch.setattr(state, "agent_app", agent_app)

    # fast.agents.get(agent_name) must be truthy
    fake_fast = MagicMock()
    fake_fast.agents = {"StaticAgent": {"config": MagicMock()}}
    monkeypatch.setattr(inject_mod, "fast", fake_fast)

    async with _make_client() as client:
        resp = await client.post(
            "/api/agents/StaticAgent/inject",
            json={"message": "hello", "priority": "normal"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "responded"
    assert body["path"] == "generate"
    assert body["agent_name"] == "StaticAgent"
    assert body["response"] == "static reply"
    fake_agent.generate.assert_awaited_once()
    # Guard against hook leak: _inject_via_generate attaches merged
    # progress+pause hooks for the call then must restore the original
    # (None here). If the restore step regresses, the next request
    # would inherit stale hooks.
    assert fake_agent.tool_runner_hooks is None, (
        "tool_runner_hooks not restored after generate path — "
        f"got {fake_agent.tool_runner_hooks!r}"
    )


# ─────────────────────────────────────────────────────────────
# 404: unknown agent
# ─────────────────────────────────────────────────────────────


async def test_inject_agent_not_found_returns_404(monkeypatch):
    """No spawn record AND not in fast.agents → 404."""
    import services.shared_state as state
    import routes.inject as inject_mod

    fake_registry = MagicMock()
    fake_registry.find_by_name = MagicMock(return_value=[])
    monkeypatch.setattr(state, "registry_db", fake_registry)

    fake_fast = MagicMock()
    fake_fast.agents = {}  # empty — .get() returns None
    monkeypatch.setattr(inject_mod, "fast", fake_fast)

    async with _make_client() as client:
        resp = await client.post(
            "/api/agents/GhostAgent/inject",
            json={"message": "anybody home?", "priority": "normal"},
        )

    assert resp.status_code == 404, resp.text
    assert "GhostAgent" in resp.json().get("detail", "")


# ─────────────────────────────────────────────────────────────
# 409: resumable but missing original_config
# ─────────────────────────────────────────────────────────────


async def test_inject_idle_without_original_config_returns_409(monkeypatch):
    """Idle/completed spawn record without original_config → 409."""
    import services.shared_state as state

    fake_record = {
        "agent_name": "OrphanAgent",
        "status": "completed",
        "started_at": 3.0,
        "original_config": None,
    }
    fake_registry = MagicMock()
    fake_registry.find_by_name = MagicMock(return_value=[fake_record])
    monkeypatch.setattr(state, "registry_db", fake_registry)

    async with _make_client() as client:
        resp = await client.post(
            "/api/agents/OrphanAgent/inject",
            json={"message": "resume me", "priority": "normal"},
        )

    assert resp.status_code == 409, resp.text
    detail = resp.json().get("detail", "")
    assert "no saved config" in detail.lower()


# ─────────────────────────────────────────────────────────────
# Auth: wrong Bearer token is rejected
# ─────────────────────────────────────────────────────────────


async def test_inject_requires_auth(monkeypatch):
    """When JARVIS_API_KEY is configured, a bad Bearer token returns 401."""
    from core import auth as core_auth

    # Activate auth at module level (verify_api_key captures this variable)
    monkeypatch.setenv("JARVIS_API_KEY", "real-key")
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "real-key")

    from server import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer wrong-key"},
    ) as client:
        resp = await client.post(
            "/api/agents/AnyAgent/inject",
            json={"message": "sneaky", "priority": "normal"},
        )

    assert resp.status_code == 401, resp.text
