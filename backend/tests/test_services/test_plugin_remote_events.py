"""Credential invalidation is durable and emits one safe push, never polling."""

from unittest.mock import Mock

import pytest
from mcp.shared.auth import OAuthToken
from sqlalchemy import create_engine

from services.plugins.remote_auth import RemoteTokenStore
from services.plugins.remote_events import notify_remote_disconnect


def test_worker_uses_existing_event_socket(monkeypatch):
    import fast_agent.spawn.spawn_events as events

    send = Mock()
    monkeypatch.setenv("SPAWN_EVENT_SOCKET", "/tmp/test-only.sock")
    monkeypatch.setattr(events, "emit_event", send)
    notify_remote_disconnect("candidate", "rovo")
    send.assert_called_once_with(
        "plugin_status",
        "",
        "",
        id="candidate",
        remote_server="rovo",
        remote_status="disconnected",
    )


@pytest.mark.asyncio
async def test_duplicate_and_stale_invalidation_do_not_emit(monkeypatch, tmp_path):
    import services.plugins.remote_events as events

    send = Mock()
    monkeypatch.setattr(events, "notify_remote_disconnect", send)
    monkeypatch.setenv("JARVIS_MASTER_KEY", "test-only")
    store = RemoteTokenStore(
        create_engine(f"sqlite:///{tmp_path / 'auth.db'}"),
        "candidate",
        "rovo",
        "https://mcp.atlassian.com/v1/mcp/authv2",
    )
    await store.set_tokens(OAuthToken(access_token="old", token_type="Bearer"))
    old = store.read()["tokens"]
    await store.set_tokens(OAuthToken(access_token="rotated", token_type="Bearer"))
    assert not store.invalidate_tokens(expected=old, compare=True)
    send.assert_not_called()
    assert store.invalidate_tokens(expected=store.read()["tokens"], compare=True)
    assert not store.invalidate_tokens()
    send.assert_called_once_with("candidate", "rovo")
    assert await store.get_tokens() is None
