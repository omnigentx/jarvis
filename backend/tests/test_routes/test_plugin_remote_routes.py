"""Remote-account routes enforce auth and cannot revoke a live binding."""

from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from core.auth import verify_api_key
from routes.plugins import get_installer
from routes.plugin_remote import router


@pytest.fixture
def client():
    lifecycle = MagicMock()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_installer] = lambda: SimpleNamespace(
        lifecycle=lifecycle
    )
    app.dependency_overrides[verify_api_key] = lambda: True
    with TestClient(app) as browser:
        yield browser, lifecycle, app


def test_connection_and_status_require_auth(client):
    browser, lifecycle, app = client

    async def unauthorized():
        raise HTTPException(401, "Unauthorized")

    app.dependency_overrides[verify_api_key] = unauthorized
    assert browser.get("/api/plugins/example/remote").status_code == 401
    assert (
        browser.post(
            "/api/plugins/example/remote/connect",
            json={
                "server": "rovo",
                "redirect_uri": "http://127.0.0.1/api/plugins/oauth/callback",
            },
        ).status_code
        == 401
    )
    assert browser.delete("/api/plugins/example/remote").status_code == 401
    lifecycle.get.assert_not_called()


def test_callback_unknown_state_is_redacted_and_not_cached(client):
    browser, _, _ = client
    result = browser.get(
        "/api/plugins/oauth/callback", params={"state": "wrong", "code": "private-code"}
    )
    assert result.status_code == 400
    assert "private-code" not in result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["referrer-policy"] == "no-referrer"


def test_reconnect_rejects_live_bindings_before_contacting_remote(client):
    browser, lifecycle, _ = client
    lifecycle.get.return_value = {"status": "ready", "bindings": [{"status": "ready"}]}
    result = browser.post(
        "/api/plugins/example/remote/connect",
        json={
            "server": "rovo",
            "redirect_uri": "http://127.0.0.1/api/plugins/oauth/callback",
        },
    )
    assert result.status_code == 409
    lifecycle.package_path.assert_not_called()


def test_disconnect_clears_token_policy_and_pushes_state(client, tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from services.plugins.policy import PluginPolicyStore
    from services.plugins.remote_auth import RemoteTokenStore, REMOTE_POLICY
    from services.activity_stream import activity_stream_manager
    from unittest.mock import Mock

    browser, lifecycle, _ = client
    monkeypatch.setenv("JARVIS_MASTER_KEY", "local-test-only")
    lifecycle.engine = create_engine("sqlite:///" + str(tmp_path / "auth.db"))
    lifecycle.operation_lock.return_value = tmp_path / "operation.lock"
    lifecycle.get.return_value = {
        "status": "downloaded",
        "bindings": [],
        "servers": {"rovo": {}},
    }
    store = RemoteTokenStore(
        lifecycle.engine, "example", "rovo", "https://mcp.atlassian.com/v1/mcp/authv2"
    )
    store.write("tokens", '{"access_token":"secret","token_type":"Bearer"}')
    PluginPolicyStore(lifecycle.engine).put("example", REMOTE_POLICY, {})
    pushed = Mock()
    monkeypatch.setattr(activity_stream_manager, "broadcast", pushed)
    result = browser.delete("/api/plugins/example/remote")
    assert result.status_code == 200
    assert not store.read()
    assert PluginPolicyStore(lifecycle.engine).get("example") is None
    data = pushed.call_args[0][0]["data"]
    assert data["remote_status"] == "disconnected"
    assert data["policy_configured"] is False
