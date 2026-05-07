"""Unit tests for services.mcp_attachments."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.database import (
    AgentMcpAttachmentModel,
    McpEventLogModel,
    McpServerModel,
    SessionLocal,
)
from services import mcp_attachments


@pytest.fixture(autouse=True)
def _per_test_db_isolation(mcp_db_isolation):
    # Defer to the shared SAVEPOINT-based fixture in conftest.py.
    yield


def _seed_servers(*names: str) -> None:
    with SessionLocal() as db:
        for n in names:
            db.add(McpServerModel(
                name=n, transport="stdio", command="python",
                args_json="[]", env_json="{}", is_builtin=False,
            ))
        db.commit()


@dataclass
class _FakeAgentConfig:
    name: str
    servers: list[str] = field(default_factory=list)


# ── seed_from_decorator ───────────────────────────────────────────────


def test_seed_from_decorator_inserts_unique_pairs():
    _seed_servers("alpha", "beta", "gamma")
    fast = MagicMock()
    fast.agents = {
        "Personal": {"config": _FakeAgentConfig(name="Personal", servers=["alpha", "beta"])},
        "IoT": {"config": _FakeAgentConfig(name="IoT", servers=["beta", "gamma"])},
    }
    res = mcp_attachments.seed_from_decorator(fast)
    assert res == {"inserted": 4, "skipped": 0}

    snapshot = mcp_attachments.list_all()
    assert snapshot == {
        "Personal": ["alpha", "beta"],
        "IoT": ["beta", "gamma"],
    }


def test_seed_from_decorator_skips_unknown_servers(caplog):
    _seed_servers("alpha")
    fast = MagicMock()
    fast.agents = {
        "Personal": {"config": _FakeAgentConfig(name="Personal", servers=["alpha", "ghost"])},
    }
    res = mcp_attachments.seed_from_decorator(fast)
    assert res == {"inserted": 1, "skipped": 0}
    assert mcp_attachments.list_for_agent("Personal") == ["alpha"]


def test_seed_from_decorator_idempotent():
    _seed_servers("alpha")
    fast = MagicMock()
    fast.agents = {"Personal": {"config": _FakeAgentConfig(name="Personal", servers=["alpha"])}}
    mcp_attachments.seed_from_decorator(fast)
    res2 = mcp_attachments.seed_from_decorator(fast)
    assert res2 == {"inserted": 0, "skipped": 1}


# ── apply_to_runtime ──────────────────────────────────────────────────


def test_apply_to_runtime_overrides_cfg_servers():
    _seed_servers("alpha", "beta", "delta")
    with SessionLocal() as db:
        db.add(AgentMcpAttachmentModel(agent_name="Personal", server_name="delta"))
        db.commit()
    cfg = _FakeAgentConfig(name="Personal", servers=["alpha", "beta"])  # decoder seed
    fast = MagicMock()
    fast.agents = {"Personal": {"config": cfg}}

    n = mcp_attachments.apply_to_runtime(fast)
    assert n == 1
    assert cfg.servers == ["delta"]  # DB wins over decorator


def test_apply_to_runtime_clears_when_no_attachments():
    _seed_servers("alpha")
    cfg = _FakeAgentConfig(name="Standalone", servers=["alpha"])
    fast = MagicMock()
    fast.agents = {"Standalone": {"config": cfg}}

    mcp_attachments.apply_to_runtime(fast)
    assert cfg.servers == []  # no row in DB → empty list


# ── attach / detach (live aggregator path mocked) ─────────────────────


@pytest.mark.asyncio
async def test_attach_persists_and_calls_aggregator():
    _seed_servers("github")
    fake_agg = MagicMock()
    fake_result = MagicMock()
    fake_result.tools_added = ["github-create_issue"]
    fake_result.tools_total = 5
    fake_result.warnings = []
    fake_result.already_attached = False
    fake_agg.attach_server = AsyncMock(return_value=fake_result)

    with patch.object(mcp_attachments, "_get_aggregator", return_value=(fake_agg, None)):
        result = await mcp_attachments.attach("Personal", "github")

    assert result["persisted"] is True
    assert result["live_attached"] is True
    assert result["tools_added"] == ["github-create_issue"]
    fake_agg.attach_server.assert_awaited_once()

    assert mcp_attachments.list_for_agent("Personal") == ["github"]


@pytest.mark.asyncio
async def test_attach_persists_even_if_runtime_unavailable():
    _seed_servers("github")
    with patch.object(mcp_attachments, "_get_aggregator", return_value=(None, "agent_app not initialized")):
        result = await mcp_attachments.attach("Personal", "github")
    assert result["persisted"] is True
    assert result["live_attached"] is False
    assert result["warning"] == "agent_app not initialized"
    assert mcp_attachments.list_for_agent("Personal") == ["github"]


@pytest.mark.asyncio
async def test_attach_unknown_server_raises():
    with patch.object(mcp_attachments, "_get_aggregator", return_value=(None, "x")):
        with pytest.raises(LookupError, match="not in catalog"):
            await mcp_attachments.attach("Personal", "ghost")


@pytest.mark.asyncio
async def test_detach_calls_aggregator_and_removes_row():
    _seed_servers("github")
    with SessionLocal() as db:
        db.add(AgentMcpAttachmentModel(agent_name="Personal", server_name="github"))
        db.commit()

    fake_agg = MagicMock()
    fake_detach = MagicMock(detached=True, tools_removed=["github-create_issue"])
    fake_agg.detach_server = AsyncMock(return_value=fake_detach)

    with patch.object(mcp_attachments, "_get_aggregator", return_value=(fake_agg, None)):
        result = await mcp_attachments.detach("Personal", "github")

    assert result["live_detached"] is True
    assert result["tools_removed"] == ["github-create_issue"]
    fake_agg.detach_server.assert_awaited_once_with("github")
    assert mcp_attachments.list_for_agent("Personal") == []


@pytest.mark.asyncio
async def test_detach_continues_if_live_detach_fails():
    """Live detach failure must NOT block DB cleanup."""
    _seed_servers("github")
    with SessionLocal() as db:
        db.add(AgentMcpAttachmentModel(agent_name="Personal", server_name="github"))
        db.commit()

    fake_agg = MagicMock()
    fake_agg.detach_server = AsyncMock(side_effect=RuntimeError("connection broken"))

    with patch.object(mcp_attachments, "_get_aggregator", return_value=(fake_agg, None)):
        result = await mcp_attachments.detach("Personal", "github")

    # Row deleted despite live detach failure
    assert mcp_attachments.list_for_agent("Personal") == []
    # Live status reported false
    assert result["live_detached"] is False


# ── reconnect_all_for_server ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconnect_all_per_agent_isolation():
    """One agent failing must not block the rest."""
    _seed_servers("github")
    with SessionLocal() as db:
        db.add(AgentMcpAttachmentModel(agent_name="A1", server_name="github"))
        db.add(AgentMcpAttachmentModel(agent_name="A2", server_name="github"))
        db.commit()

    good_agg = MagicMock()
    good_agg.detach_server = AsyncMock()
    good_attach = MagicMock(tools_total=3, warnings=[], tools_added=["x"], already_attached=False)
    good_agg.attach_server = AsyncMock(return_value=good_attach)

    bad_agg = MagicMock()
    bad_agg.detach_server = AsyncMock()
    bad_agg.attach_server = AsyncMock(side_effect=RuntimeError("oauth failed"))

    def _get_agg(agent_name: str):
        return (good_agg if agent_name == "A1" else bad_agg, None)

    with patch.object(mcp_attachments, "_get_aggregator", side_effect=_get_agg):
        result = await mcp_attachments.reconnect_all_for_server("github")

    assert result["all_ok"] is False
    by_agent = {r["agent"]: r for r in result["agents"]}
    assert by_agent["A1"]["ok"] is True
    assert by_agent["A2"]["ok"] is False
    assert "oauth failed" in by_agent["A2"]["error"]


@pytest.mark.asyncio
async def test_reconnect_all_no_agents_attached():
    _seed_servers("github")
    result = await mcp_attachments.reconnect_all_for_server("github")
    assert result == {"server": "github", "agents": [], "all_ok": True}
