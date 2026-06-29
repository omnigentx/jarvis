"""Liveness/readiness contract after the startup-decouple change.

The lifespan brings the FastAgent runtime up in the BACKGROUND so uvicorn serves
immediately (the CD probe no longer false-fails on a slow first-deploy model
download). Two invariants make that safe:
  1. the liveness probe must NOT depend on agent_app, and
  2. agent_app-dependent routes must return 503 — not crash — during the brief
     window before the background runtime sets state.agent_app.
"""
import pytest


async def test_auth_probe_does_not_depend_on_agent_app(monkeypatch):
    """The CD liveness probe answers even before agents are ready — this is what
    lets the deploy succeed while models/agents warm up in the background."""
    import services.shared_state as ss
    monkeypatch.setattr(ss, "agent_app", None)
    from routes.setup import auth_probe
    res = await auth_probe()
    assert hasattr(res, "configured")   # returns AuthProbe; never touched agent_app


async def test_chat_returns_503_until_agent_app_ready(monkeypatch):
    import services.shared_state as ss
    monkeypatch.setattr(ss, "agent_app", None)
    from fastapi import HTTPException
    from routes.chat import ChatRequest, chat
    with pytest.raises(HTTPException) as ei:
        await chat(ChatRequest(message="hi"))
    assert ei.value.status_code == 503


async def test_chat_stream_returns_503_until_agent_app_ready(monkeypatch):
    import services.shared_state as ss
    monkeypatch.setattr(ss, "agent_app", None)
    from fastapi import HTTPException
    from routes.chat import chat_stream
    with pytest.raises(HTTPException) as ei:
        await chat_stream(raw_request=None)
    assert ei.value.status_code == 503


async def test_readiness_probe_503_until_agent_app_ready(monkeypatch):
    """The READINESS probe (distinct from liveness) stays 503 while the background
    runtime hasn't set agent_app — so a never-ready backend is NOT a silent-green
    deploy (CD/monitoring can alert on it)."""
    from fastapi import Response
    import services.shared_state as ss
    monkeypatch.setattr(ss, "agent_app", None)
    from routes.setup import readiness_probe
    resp = Response()
    result = await readiness_probe(resp)
    assert resp.status_code == 503
    assert result.ready is False


async def test_readiness_probe_200_when_agents_ready(monkeypatch):
    from unittest.mock import MagicMock
    from fastapi import Response
    import services.shared_state as ss
    monkeypatch.setattr(ss, "agent_app",
                        MagicMock(_agents={"Jarvis": object(), "IoTAgent": object()}))
    from routes.setup import readiness_probe
    resp = Response()
    result = await readiness_probe(resp)
    assert result.ready is True and result.agents == 2
