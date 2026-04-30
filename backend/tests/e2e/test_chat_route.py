"""E2E tests for backend/routes/chat.py.

Covers:
  1. POST /api/chat — sync happy path.
  2. POST /api/chat-stream — SSE event shape (start + done).
  3. POST /api/chat-stream — resume_and_send raises mid-stream, SSE 'error'.
  4. POST /api/chat-stream — two concurrent sessions, isolated payloads.
  5. POST /api/chat — setup gate closed returns 503.

Tests stub ``services.session_service.SessionService.resume_and_send`` at the
singleton level (the same instance imported by ``routes.chat``) so we exercise
the real route handler — request parsing, SSE generator, setup gate — without
standing up a real agent pipeline or DB.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest


# ────────────────────────────────────────────────────────────────────
# Module-level cleanup
# ────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_server_env_leak():
    """Importing ``server`` sets SPAWN_REGISTRY_DB / SPAWN_PROJECT_DIR via
    ``os.environ.setdefault`` at module top-level. That leaks into unrelated
    tests (e.g. ``test_subprocess_env_vars``). Snapshot + restore around each
    test so we stay a good citizen in the suite.
    """
    snapshot = {k: os.environ.get(k) for k in ("SPAWN_REGISTRY_DB", "SPAWN_PROJECT_DIR")}
    try:
        yield
    finally:
        for k, v in snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _open_setup_gate(monkeypatch) -> None:
    """Force the setup gate middleware to treat setup as complete so API
    requests aren't short-circuited with 503 before reaching the handler."""
    from middleware import setup_gate

    monkeypatch.setattr(setup_gate, "is_setup_complete", lambda: True)


def _patch_session_service(
    monkeypatch,
    *,
    resume_return: Any = None,
    resume_side_effect: Exception | None = None,
) -> list[dict]:
    """Swap ``session_service.resume_and_send`` and ``ensure_session`` to
    deterministic stubs. Returns a list that will be filled with one dict per
    resume_and_send call, capturing (message, conversation_id, agent_name)
    so tests can assert routing/isolation.
    """
    from services import shared_state

    calls: list[dict] = []

    async def _fake_resume(
        agent_app,
        message: str,
        session_id: str | None,
        files_data=None,
        agent_name=None,
    ):
        calls.append({
            "message": message,
            "conversation_id": session_id,
            "agent_name": agent_name,
        })
        if resume_side_effect is not None:
            raise resume_side_effect
        if callable(resume_return):
            return resume_return(message, session_id)
        return resume_return

    def _fake_ensure(session_id):
        # Preserve caller-supplied id; generate deterministic fallback when None.
        return session_id or "new-session-id"

    monkeypatch.setattr(
        shared_state.session_service, "resume_and_send", _fake_resume, raising=True
    )
    monkeypatch.setattr(
        shared_state.session_service, "ensure_session", _fake_ensure, raising=True
    )
    return calls


async def _collect_sse_events(response, *, max_events: int = 20, timeout: float = 10.0) -> list[dict]:
    """Consume an SSE response, returning each ``data:`` payload parsed as JSON.

    Stops when a ``done`` or ``error`` event arrives, or when ``max_events``
    payloads are collected. Uses asyncio.wait_for so a stuck stream fails loud
    instead of hanging the test runner.
    """
    events: list[dict] = []

    def _pop_frame(buf: str) -> tuple[str | None, str]:
        # sse-starlette emits CRLF by default; accept both to stay robust.
        for sep in ("\r\n\r\n", "\n\n"):
            if sep in buf:
                frame, rest = buf.split(sep, 1)
                return frame, rest
        return None, buf

    async def _drain() -> None:
        buf = ""
        async for chunk in response.aiter_text():
            buf += chunk
            while True:
                frame, buf = _pop_frame(buf)
                if frame is None:
                    break
                for line in frame.splitlines():
                    if line.startswith("data:"):
                        payload = line[len("data:"):].strip()
                        if not payload:
                            continue
                        try:
                            evt = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        events.append(evt)
                        if evt.get("type") in ("done", "error"):
                            return
                        if len(events) >= max_events:
                            return

    await asyncio.wait_for(_drain(), timeout=timeout)
    return events


# ────────────────────────────────────────────────────────────────────
# Test 1: /api/chat happy path
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_sync_simple_happy_path(app_client, monkeypatch):
    _open_setup_gate(monkeypatch)
    _patch_session_service(
        monkeypatch,
        resume_return=("xin chào bạn", "test-conv-1"),
    )

    resp = await app_client.post(
        "/api/chat",
        json={"message": "xin chào", "conversation_id": "test-conv-1"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["response"] == "xin chào bạn"
    assert body["conversation_id"] == "test-conv-1"


# ────────────────────────────────────────────────────────────────────
# Test 2: /api/chat-stream event shape
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_stream_sse_event_sequence(app_client, monkeypatch):
    _open_setup_gate(monkeypatch)
    _patch_session_service(
        monkeypatch,
        resume_return=("kết quả tìm X", "c2"),
    )

    async with app_client.stream(
        "POST",
        "/api/chat-stream",
        json={"message": "tìm X", "conversation_id": "c2"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = await _collect_sse_events(response, timeout=10.0)

    event_types = [e.get("type") for e in events]
    assert "start" in event_types, f"missing 'start' event; got {event_types}"
    assert event_types[-1] == "done", (
        f"stream must end with 'done'; got tail {event_types[-3:]}"
    )

    done = events[-1]
    # The done event payload is nested under 'data' by _make_event.
    data = done.get("data", done)
    assert "response" in data
    assert "conversation_id" in data
    assert "total_tokens" in data
    assert data["response"] == "kết quả tìm X"
    assert data["conversation_id"] == "c2"


# ────────────────────────────────────────────────────────────────────
# Test 3: mid-stream error path
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_stream_error_mid_stream(app_client, monkeypatch):
    _open_setup_gate(monkeypatch)
    _patch_session_service(
        monkeypatch,
        resume_side_effect=RuntimeError("agent crashed"),
    )

    async with app_client.stream(
        "POST",
        "/api/chat-stream",
        json={"message": "boom", "conversation_id": "c-err"},
    ) as response:
        assert response.status_code == 200
        events = await _collect_sse_events(response, timeout=10.0)

    types = [e.get("type") for e in events]
    assert "error" in types, f"expected 'error' event, got {types}"
    error_evt = next(e for e in events if e.get("type") == "error")
    msg = error_evt.get("data", error_evt).get("message", "")
    assert "agent crashed" in msg, (
        f"error event message must surface the underlying exception; got {msg!r}"
    )


# ────────────────────────────────────────────────────────────────────
# Test 4: concurrent sessions stay isolated
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_stream_concurrent_sessions_isolated(app_client, monkeypatch):
    _open_setup_gate(monkeypatch)

    # Route each call to a distinct reply keyed by the incoming conversation_id.
    replies = {
        "c-a": "reply for A",
        "c-b": "reply for B",
    }

    async def _return_per_cid(message: str, session_id: str | None):
        # Introduce a short await so the two tasks actually interleave on the
        # event loop — otherwise the first request could finish before the
        # second even starts and the test wouldn't prove isolation.
        await asyncio.sleep(0.05)
        return replies[session_id], session_id

    from services import shared_state

    async def _fake_resume(
        agent_app,
        message,
        session_id,
        files_data=None,
        agent_name=None,
    ):
        return await _return_per_cid(message, session_id)

    monkeypatch.setattr(
        shared_state.session_service, "resume_and_send", _fake_resume, raising=True
    )
    monkeypatch.setattr(
        shared_state.session_service,
        "ensure_session",
        lambda sid: sid or "new",
        raising=True,
    )

    async def _run(cid: str) -> dict:
        async with app_client.stream(
            "POST",
            "/api/chat-stream",
            json={"message": f"ping {cid}", "conversation_id": cid},
        ) as response:
            assert response.status_code == 200
            events = await _collect_sse_events(response, timeout=10.0)
        done = next(e for e in events if e.get("type") == "done")
        return done.get("data", done)

    results = await asyncio.gather(_run("c-a"), _run("c-b"))

    data_a, data_b = results
    assert data_a["conversation_id"] == "c-a"
    assert data_a["response"] == "reply for A"
    assert data_b["conversation_id"] == "c-b"
    assert data_b["response"] == "reply for B"


# ────────────────────────────────────────────────────────────────────
# Test 5: setup gate closed → 503
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_setup_gate_blocks_chat_before_setup_complete(app_client, monkeypatch):
    from middleware import setup_gate

    # Explicitly close the gate — the middleware caches state at import time
    # and tests before this one may have warmed it open.
    monkeypatch.setattr(setup_gate, "is_setup_complete", lambda: False)

    resp = await app_client.post(
        "/api/chat",
        json={"message": "hi", "conversation_id": "whatever"},
    )

    assert resp.status_code == 503
    assert resp.headers.get("X-Setup-Required") == "true"
    body = resp.json()
    assert body.get("error") == "setup_required"
