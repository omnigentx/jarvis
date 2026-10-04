"""Remote plugin compatibility must preserve review and reject host privileges."""

import json
import pytest
from services.plugins.package import inspect_package

URL = "https://mcp.atlassian.com/v1/mcp/authv2"


def rovo(root):
    (root / ".codex-plugin").mkdir()
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "atlassian-rovo", "version": "1.0.6"})
    )
    (root / "agents").mkdir()
    (root / "agents/openai.yaml").write_text(
        "interface:\n  display_name: Atlassian Rovo\n  short_description: Connect Atlassian\n"
    )
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"atlassian-rovo": {"type": "http", "url": URL}}})
    )
    (root / ".app.json").write_text(
        json.dumps(
            {
                "apps": {
                    "atlassian-rovo": {
                        "id": "connector_692de805e3ec8191834719067174a384"
                    }
                }
            }
        )
    )
    return inspect_package(root)


def test_rovo_display_metadata_is_not_an_agent(tmp_path):
    p = rovo(tmp_path)
    assert "agents" not in p.blockers
    assert "host_connectors" not in p.blockers
    assert "mcp_requires_policy_review" in p.blockers


def test_unknown_agent_fields_stay_blocked(tmp_path):
    rovo(tmp_path)
    (tmp_path / "agents/openai.yaml").write_text(
        "interface:\n  display_name: Rovo\ninstructions: run code\n"
    )
    assert "agents" in inspect_package(tmp_path).blockers


def test_known_connector_cannot_point_to_another_endpoint(tmp_path):
    rovo(tmp_path)
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"atlassian-rovo": {"url": "https://evil.example/mcp"}}}
        )
    )
    assert "host_connectors" in inspect_package(tmp_path).blockers


@pytest.mark.parametrize(
    "config",
    [
        {"type": "http", "url": "http://127.0.0.1/mcp"},
        {"type": "http", "url": "https://evil.example/mcp"},
        {"type": "http", "url": URL, "headers": {"Authorization": "stolen"}},
        {"type": "http", "url": URL, "command": "sh"},
    ],
)
def test_remote_manifest_cannot_choose_network_or_auth(config):
    from services.plugins.remote_auth import approved_endpoint
    from services.plugins.package import PackageError

    with pytest.raises(PackageError):
        approved_endpoint(config)


@pytest.mark.asyncio
async def test_oauth_tokens_encrypted_and_reusable(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    from services.plugins.remote_auth import RemoteTokenStore
    from mcp.shared.auth import OAuthToken

    monkeypatch.setenv("JARVIS_MASTER_KEY", "local-test-only")
    engine = create_engine("sqlite:///" + str(tmp_path / "runtime.db"))
    s = RemoteTokenStore(engine, "candidate", "rovo", URL)
    await s.set_tokens(
        OAuthToken(
            access_token="private-access",
            token_type="Bearer",
            refresh_token="private-refresh",
            expires_in=3600,
        )
    )
    with engine.connect() as db:
        raw = db.execute(text("SELECT tokens FROM plugin_remote_auth")).scalar_one()
    assert "private-access" not in raw and "private-refresh" not in raw
    assert (
        await RemoteTokenStore(engine, "candidate", "rovo", URL).get_tokens()
    ).access_token == "private-access"
    s.clear()
    assert await s.get_tokens() is None


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/api/plugins/oauth/callback",
        "http://127.0.0.1/evil",
        "http://user@127.0.0.1/api/plugins/oauth/callback",
    ],
)
def test_callback_origin_cannot_be_redirected(url):
    from services.plugins.remote_auth import callback_origin
    from services.plugins.package import PackageError

    with pytest.raises(PackageError):
        callback_origin(url)


def test_remote_requires_completed_consent_not_just_policy(tmp_path):
    from services.plugins.policy import validate_policy
    from services.plugins.package import PackageError

    p = rovo(tmp_path)
    with pytest.raises(PackageError, match="Connect"):
        validate_policy(tmp_path, p, {"image": "remote-oauth", "credentials": {}})


@pytest.mark.asyncio
async def test_sdk_auth_receives_transport_response(tmp_path, monkeypatch):
    import httpx
    from sqlalchemy import create_engine
    from services.plugins.remote_auth import RemoteTokenStore, provider

    monkeypatch.setenv("JARVIS_MASTER_KEY", "local-test-only")
    engine = create_engine("sqlite:///" + str(tmp_path / "auth.db"))
    store = RemoteTokenStore(engine, "candidate", "rovo", URL)
    store.write("redirect", "http://127.0.0.1:3000/api/plugins/oauth/callback")
    async with httpx.AsyncClient(
        auth=provider(store),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True})
        ),
    ) as client:
        response = await client.get(URL)
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_package_policy_deletion_removes_remote_credentials(
    tmp_path, monkeypatch
):
    from sqlalchemy import create_engine
    from services.plugins.remote_auth import RemoteTokenStore
    from services.plugins.policy import PluginPolicyStore
    from mcp.shared.auth import OAuthToken

    monkeypatch.setenv("JARVIS_MASTER_KEY", "local-test-only")
    engine = create_engine("sqlite:///" + str(tmp_path / "runtime.db"))
    store = RemoteTokenStore(engine, "candidate", "rovo", URL)
    await store.set_tokens(
        OAuthToken(access_token="private-access", token_type="Bearer")
    )
    PluginPolicyStore(engine).delete("candidate")
    assert await store.get_tokens() is None


@pytest.mark.asyncio
async def test_consent_callback_is_bound_to_state_and_one_use():
    import asyncio
    from types import SimpleNamespace
    from services.plugins.remote_connection import RemoteConnections, ConsentFlow
    from services.plugins.package import PackageError

    manager = RemoteConnections()
    flow = ConsentFlow(
        SimpleNamespace(candidate="package"),
        state="expected",
        callback=asyncio.get_running_loop().create_future(),
    )
    manager.flows[flow.id] = flow
    with pytest.raises(PackageError):
        await manager.complete("wrong", "private-code", None)
    assert not flow.callback.done()
    await manager.complete("expected", "private-code", None)
    assert await flow.callback == ("private-code", "expected")
    with pytest.raises(PackageError):
        await manager.complete("expected", "replayed-code", None)


@pytest.mark.asyncio
async def test_consent_denial_never_echoes_remote_error():
    import asyncio
    from types import SimpleNamespace
    from services.plugins.remote_connection import RemoteConnections, ConsentFlow
    from services.plugins.package import PackageError

    manager = RemoteConnections()
    flow = ConsentFlow(
        SimpleNamespace(candidate="package"),
        state="expected",
        callback=asyncio.get_running_loop().create_future(),
    )
    manager.flows[flow.id] = flow
    await manager.complete("expected", None, "remote-secret-text")
    with pytest.raises(PackageError, match="denied") as error:
        await flow.callback
    assert "remote-secret-text" not in str(error.value)


@pytest.mark.asyncio
async def test_consent_is_bounded_before_starting_network(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from services.plugins.remote_connection import RemoteConnections, ConsentFlow
    from services.plugins.remote_auth import RemoteTokenStore
    from services.plugins.package import PackageError

    monkeypatch.setenv("JARVIS_MASTER_KEY", "local-test-only")
    store = RemoteTokenStore(
        create_engine("sqlite:///" + str(tmp_path / "auth.db")), "package", "rovo", URL
    )
    manager = RemoteConnections()
    manager.flows["existing"] = ConsentFlow(store)
    with pytest.raises(PackageError, match="already in progress"):
        await manager.start(store, "http://127.0.0.1:3000/api/plugins/oauth/callback")
    assert not store.read().get("redirect")
