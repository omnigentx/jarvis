"""Unit tests for services.mcp_catalog."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from core.database import (
    AgentMcpAttachmentModel,
    Base,
    McpEventLogModel,
    McpServerModel,
    SessionLocal,
    engine,
)
from services import mcp_catalog


@pytest.fixture(autouse=True)
def _clean_tables():
    Base.metadata.create_all(bind=engine)
    yield
    with SessionLocal() as db:
        db.query(AgentMcpAttachmentModel).delete()
        db.query(McpServerModel).delete()
        db.query(McpEventLogModel).delete()
        db.commit()


def _make_yaml(tmp_path: Path, mcp_servers: dict) -> Path:
    p = tmp_path / "fastagent.config.yaml"
    p.write_text(yaml.dump({"mcp": {"servers": mcp_servers}}))
    return p


# ── seed_from_yaml ────────────────────────────────────────────────────


def test_seed_from_yaml_inserts_missing(tmp_path):
    cfg = _make_yaml(tmp_path, {
        "alpha": {"command": "python", "args": ["a.py"]},
        "beta": {"url": "https://b.example/mcp", "transport": "http"},
    })
    secrets = tmp_path / "fastagent.secrets.yaml"
    result = mcp_catalog.seed_from_yaml(cfg, secrets)
    assert result == {"inserted": 2, "skipped": 0}

    with SessionLocal() as db:
        rows = db.query(McpServerModel).all()
    by_name = {r.name: r for r in rows}
    assert set(by_name.keys()) == {"alpha", "beta"}
    assert by_name["alpha"].is_builtin is True
    assert by_name["alpha"].transport == "stdio"
    assert json.loads(by_name["alpha"].args_json) == ["a.py"]
    assert by_name["beta"].transport == "http"
    assert by_name["beta"].url == "https://b.example/mcp"


def test_seed_from_yaml_idempotent_on_second_call(tmp_path):
    cfg = _make_yaml(tmp_path, {"alpha": {"command": "python"}})
    secrets = tmp_path / "fastagent.secrets.yaml"
    first = mcp_catalog.seed_from_yaml(cfg, secrets)
    second = mcp_catalog.seed_from_yaml(cfg, secrets)
    assert first == {"inserted": 1, "skipped": 0}
    assert second == {"inserted": 0, "skipped": 1}


def test_seed_from_yaml_does_not_overwrite_user_edits(tmp_path):
    cfg = _make_yaml(tmp_path, {"alpha": {"command": "python", "args": ["original.py"]}})
    secrets = tmp_path / "fastagent.secrets.yaml"
    mcp_catalog.seed_from_yaml(cfg, secrets)

    # Simulate user edit: change command via DB
    with SessionLocal() as db:
        row = db.get(McpServerModel, "alpha")
        row.command = "node"
        row.args_json = json.dumps(["edited.js"])
        db.commit()

    # Re-seed: should NOT clobber
    mcp_catalog.seed_from_yaml(cfg, secrets)
    with SessionLocal() as db:
        row = db.get(McpServerModel, "alpha")
    assert row.command == "node"
    assert json.loads(row.args_json) == ["edited.js"]


def test_seed_from_yaml_picks_up_new_yaml_entries(tmp_path):
    cfg = _make_yaml(tmp_path, {"alpha": {"command": "python"}})
    secrets = tmp_path / "fastagent.secrets.yaml"
    mcp_catalog.seed_from_yaml(cfg, secrets)

    cfg2 = _make_yaml(tmp_path, {
        "alpha": {"command": "python"},
        "gamma": {"command": "uv", "args": ["run", "x"]},
    })
    res = mcp_catalog.seed_from_yaml(cfg2, secrets)
    assert res == {"inserted": 1, "skipped": 1}


def test_seed_from_yaml_merges_secrets_overlay(tmp_path):
    cfg = _make_yaml(tmp_path, {
        "alpha": {"command": "python", "env": {"BASE": "base-val"}}
    })
    secrets = tmp_path / "fastagent.secrets.yaml"
    secrets.write_text(yaml.dump({
        "mcp": {"servers": {"alpha": {"env": {"TOKEN": "secret-t"}}}}
    }))
    mcp_catalog.seed_from_yaml(cfg, secrets)
    with SessionLocal() as db:
        env = json.loads(db.get(McpServerModel, "alpha").env_json)
    assert env == {"BASE": "base-val", "TOKEN": "secret-t"}


# ── validate_payload ──────────────────────────────────────────────────


def test_validate_rejects_bad_name():
    with pytest.raises(ValueError, match="invalid server name"):
        mcp_catalog.validate_payload("--bad", {"transport": "stdio", "command": "x"})


def test_validate_rejects_unknown_transport():
    with pytest.raises(ValueError, match="transport must be one of"):
        mcp_catalog.validate_payload("ok", {"transport": "ftp", "command": "x"})


def test_validate_stdio_requires_command():
    with pytest.raises(ValueError, match="stdio transport requires"):
        mcp_catalog.validate_payload("ok", {"transport": "stdio"})


def test_validate_http_requires_url():
    with pytest.raises(ValueError, match="http transport requires"):
        mcp_catalog.validate_payload("ok", {"transport": "http"})


# ── list / get with masking ───────────────────────────────────────────


def test_list_masks_secret_env_keys(tmp_path):
    with SessionLocal() as db:
        db.add(McpServerModel(
            name="srv", transport="stdio", command="python",
            args_json="[]",
            env_json=json.dumps({"PUBLIC": "visible", "GITHUB_TOKEN": "ghs_xxx", "API_KEY": "k1"}),
            is_builtin=False,
        ))
        db.commit()
    items = mcp_catalog.list_all(mask_secrets=True)
    assert len(items) == 1
    env = items[0]["env"]
    assert env["PUBLIC"] == "visible"
    assert env["GITHUB_TOKEN"] == "••••"
    assert env["API_KEY"] == "••••"


def test_list_unmasked_returns_raw_env():
    with SessionLocal() as db:
        db.add(McpServerModel(
            name="srv", transport="stdio", command="python",
            args_json="[]",
            env_json=json.dumps({"GITHUB_TOKEN": "ghs_xxx"}),
            is_builtin=False,
        ))
        db.commit()
    items = mcp_catalog.list_all(mask_secrets=False)
    assert items[0]["env"] == {"GITHUB_TOKEN": "ghs_xxx"}


def test_get_secret_value_returns_raw():
    with SessionLocal() as db:
        db.add(McpServerModel(
            name="srv", transport="stdio", command="python",
            args_json="[]",
            env_json=json.dumps({"GITHUB_TOKEN": "ghs_secret"}),
            is_builtin=False,
        ))
        db.commit()
    assert mcp_catalog.get_secret_value("srv", "GITHUB_TOKEN") == "ghs_secret"
    assert mcp_catalog.get_secret_value("srv", "MISSING") is None


# ── delete ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_built_in_is_blocked():
    with SessionLocal() as db:
        db.add(McpServerModel(
            name="builtin-srv", transport="stdio", command="python",
            args_json="[]", env_json="{}", is_builtin=True,
        ))
        db.commit()
    with pytest.raises(PermissionError):
        await mcp_catalog.delete("builtin-srv")
    with SessionLocal() as db:
        assert db.get(McpServerModel, "builtin-srv") is not None


@pytest.mark.asyncio
async def test_delete_user_server_succeeds():
    with SessionLocal() as db:
        db.add(McpServerModel(
            name="user-srv", transport="stdio", command="python",
            args_json="[]", env_json="{}", is_builtin=False,
        ))
        db.commit()
    res = await mcp_catalog.delete("user-srv")
    assert res == {"deleted": True, "name": "user-srv"}
    with SessionLocal() as db:
        assert db.get(McpServerModel, "user-srv") is None


# ── create (smoke-test mocked) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_persists_when_smoke_passes():
    async def fake_smoke(_payload, **_kw):
        return {"ok": True, "tools": ["t1", "t2"], "error": None}
    with patch.object(mcp_catalog, "smoke_test", side_effect=fake_smoke):
        result = await mcp_catalog.create("new-srv", {
            "transport": "stdio", "command": "python", "args": ["x.py"],
        })
    assert result["name"] == "new-srv"
    assert result["is_builtin"] is False
    with SessionLocal() as db:
        assert db.get(McpServerModel, "new-srv") is not None


@pytest.mark.asyncio
async def test_create_persists_cwd_and_round_trips_to_settings(tmp_path):
    """Regression: a stdio server with relative args (e.g. ['server.py']) needs
    cwd to launch correctly. cwd must be persisted in mcp_servers and pass
    through to MCPServerSettings on apply_to_registry — otherwise the live
    aggregator launches the subprocess from the backend's cwd and gets
    'McpError: Connection closed'."""
    async def fake_smoke(_payload, **_kw):
        return {"ok": True, "tools": [], "error": None}
    with patch.object(mcp_catalog, "smoke_test", side_effect=fake_smoke):
        result = await mcp_catalog.create("withcwd", {
            "transport": "stdio", "command": "python", "args": ["server.py"],
            "cwd": str(tmp_path),
        })
    assert result["cwd"] == str(tmp_path)
    with SessionLocal() as db:
        row = db.get(McpServerModel, "withcwd")
        assert row.cwd == str(tmp_path)
        settings = mcp_catalog._row_to_mcp_settings(row)
    assert getattr(settings, "cwd", None) == str(tmp_path)


def test_seed_from_yaml_carries_cwd(tmp_path):
    cfg = _make_yaml(tmp_path, {
        "alpha": {"command": "python", "args": ["s.py"], "cwd": "/srv/alpha"},
    })
    secrets = tmp_path / "fastagent.secrets.yaml"
    mcp_catalog.seed_from_yaml(cfg, secrets)
    with SessionLocal() as db:
        assert db.get(McpServerModel, "alpha").cwd == "/srv/alpha"


@pytest.mark.asyncio
async def test_update_can_patch_cwd():
    with SessionLocal() as db:
        db.add(McpServerModel(
            name="srv", transport="stdio", command="python",
            args_json='["s.py"]', env_json="{}", cwd="/old/path",
            is_builtin=False,
        ))
        db.commit()
    async def fake_smoke(_payload, **_kw):
        return {"ok": True, "tools": [], "error": None}
    with patch.object(mcp_catalog, "smoke_test", side_effect=fake_smoke):
        result = await mcp_catalog.update("srv", {"cwd": "/new/path"})
    assert result["cwd"] == "/new/path"
    with SessionLocal() as db:
        assert db.get(McpServerModel, "srv").cwd == "/new/path"


@pytest.mark.asyncio
async def test_smoke_test_returns_tool_details_when_requested(monkeypatch):
    """smoke_test(return_tool_details=True) must include {name, description} per
    tool — routes/mcp.py uses this to refresh mcp_server_tools cache for
    unattached servers (where fast-agent never connects on its own)."""

    class _FakeTool:
        def __init__(self, name, description):
            self.name = name
            self.description = description

    class _FakeSession:
        async def list_tools(self):
            class _R:
                tools = [
                    _FakeTool("send_email", "Send an email — neutral example."),
                    _FakeTool("check_inbox", ""),
                ]
            return _R()

    class _FakeConn:
        session = _FakeSession()
        async def wait_for_initialized(self):
            pass

    class _FakeCM:
        def __init__(self, registry, context=None):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def launch_server(self, name, **kw):
            return _FakeConn()
        async def disconnect_server(self, name):
            pass

    class _FakeRegistry:
        registry: dict = {}

    class _FakeContext:
        server_registry = _FakeRegistry()

    class _FakeAgent:
        context = _FakeContext()

    class _FakeAgentApp:
        # smoke_test reads agent_app._agents.values() and grabs context from any.
        _agents = {"some-agent": _FakeAgent()}

    monkeypatch.setattr("services.shared_state.agent_app", _FakeAgentApp())
    monkeypatch.setattr(
        "fast_agent.mcp.mcp_connection_manager.MCPConnectionManager", _FakeCM
    )

    res = await mcp_catalog.smoke_test(
        {"transport": "stdio", "command": "python"}, return_tool_details=True
    )
    assert res["ok"] is True
    assert res["tools"] == ["send_email", "check_inbox"]
    assert res["tool_details"] == [
        {"name": "send_email", "description": "Send an email — neutral example."},
        {"name": "check_inbox", "description": ""},
    ]


@pytest.mark.asyncio
async def test_create_does_not_persist_when_smoke_fails():
    async def fake_smoke(_payload, **_kw):
        return {"ok": False, "tools": [], "error": "missing binary"}
    with patch.object(mcp_catalog, "smoke_test", side_effect=fake_smoke):
        with pytest.raises(RuntimeError, match="smoke test failed"):
            await mcp_catalog.create("bad-srv", {
                "transport": "stdio", "command": "no-such-bin",
            })
    with SessionLocal() as db:
        assert db.get(McpServerModel, "bad-srv") is None


# ── apply_to_registry ─────────────────────────────────────────────────


def test_apply_to_registry_replaces_registry_dict():
    """Cross-layer check: DB content lands on context.server_registry.registry."""
    from fast_agent.mcp_server_registry import ServerRegistry

    with SessionLocal() as db:
        db.add(McpServerModel(
            name="from-db", transport="stdio", command="python",
            args_json=json.dumps(["script.py"]), env_json="{}",
            is_builtin=False,
        ))
        db.commit()

    registry = ServerRegistry()
    registry.registry = {"stale": object()}  # type: ignore[dict-item]

    class _FakeCtx:
        server_registry = registry

    count = mcp_catalog.apply_to_registry(_FakeCtx())
    assert count == 1
    assert "stale" not in registry.registry
    assert "from-db" in registry.registry
    settings = registry.registry["from-db"]
    assert settings.transport == "stdio"
    assert settings.command == "python"
    assert settings.args == ["script.py"]
