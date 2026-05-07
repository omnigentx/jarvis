"""Unit tests for services.mcp_rpc_handlers — focuses on the self-lockout
guard. Catalog/admin business logic is covered by their own test files.
"""
from __future__ import annotations

import pytest

from services import mcp_rpc_handlers as rpc


@pytest.mark.asyncio
async def test_self_lockout_blocks_update_of_mcp_admin():
    res = await rpc.mcp_update_server(name="mcp_admin", patch={"command": "evil"})
    assert res["status"] == 423
    assert "would lock you out" in res["error"]


@pytest.mark.asyncio
async def test_self_lockout_blocks_delete_of_mcp_admin():
    res = await rpc.mcp_delete_server(name="mcp_admin")
    assert res["status"] == 423
    assert "deleting" in res["error"]


@pytest.mark.asyncio
async def test_self_lockout_blocks_detach_of_skill_server():
    res = await rpc.mcp_detach_from_agent(server="skill_server", agent="Jarvis")
    assert res["status"] == 423


@pytest.mark.asyncio
async def test_self_lockout_does_not_block_other_servers(monkeypatch):
    """Confirm the guard is name-specific, not a blanket block."""
    captured = {}

    async def fake_detach(agent, server, actor=None):
        captured["called"] = (agent, server)
        return {"agent": agent, "server": server, "live_detached": True,
                "tools_removed": [], "warning": None}

    from services import mcp_attachments
    monkeypatch.setattr(mcp_attachments, "detach", fake_detach)

    res = await rpc.mcp_detach_from_agent(server="github", agent="Jarvis")
    assert "status" not in res or res.get("status") != 423
    assert captured["called"] == ("Jarvis", "github")


def test_self_lockout_lists_both_admin_servers():
    """If you add a new self-lockable server, update the test here too."""
    assert rpc._SELF_LOCKED == {"mcp_admin", "skill_server"}


# ── Compact projections (context-budget regression) ───────────────────


def test_list_servers_default_is_compact(monkeypatch):
    """Default mcp_list_servers must drop command/args/env so a 28-server
    catalog doesn't burn ~13K tokens of agent context. verbose=True
    keeps the legacy full payload for callers that need it."""
    fake_rows = [{
        "name": f"srv{i}", "transport": "stdio", "command": "python",
        "args": ["-m", "long.module.path.that.adds.bytes"],
        "env": {"TOKEN": "redacted", "URL": "https://example.com/api"},
        "url": None, "cwd": None, "is_builtin": (i % 2 == 0),
        "created_at": 0, "updated_at": 0,
    } for i in range(3)]
    from services import mcp_attachments, mcp_catalog
    monkeypatch.setattr(mcp_catalog, "list_all", lambda mask_secrets=True: list(fake_rows))
    monkeypatch.setattr(mcp_attachments, "list_for_server", lambda n: ["Jarvis"])

    res = rpc.mcp_list_servers()
    servers = res["servers"]
    assert len(servers) == 3
    for s in servers:
        assert set(s.keys()) == {"name", "transport", "is_builtin", "attached_agents"}
        # Bloat fields must be absent
        assert "command" not in s and "args" not in s and "env" not in s

    # verbose=True restores the full shape (used by dashboard / inspectors).
    full = rpc.mcp_list_servers(verbose=True)
    assert "command" in full["servers"][0]
    assert "args" in full["servers"][0]


def test_trim_tool_detail_caps_long_descriptions():
    long_desc = "x" * 1000
    out = rpc._trim_tool_detail({"name": "t", "description": long_desc})
    assert out["name"] == "t"
    # Cap is 240 chars + 1 ellipsis
    assert len(out["description"]) <= rpc._TOOL_DESC_CHARS + 1
    assert out["description"].endswith("…")


def test_trim_tool_detail_preserves_short_descriptions():
    out = rpc._trim_tool_detail({"name": "t", "description": "Short and useful."})
    assert out["description"] == "Short and useful."


def test_list_generated_compacts_planned_tools(monkeypatch, tmp_path):
    """planned_tools per row should collapse to just tool names — full
    detail is one mcp_get_generated(name) call away when needed."""
    fake = [{
        "name": "alpha", "description": "x", "status": "scaffolded",
        "version": "0.1.0", "language": "python",
        "planned_tools": [
            {"name": "ping", "description": "Health-check the service.",
             "args": [{"name": "n", "type": "int"}]},
            {"name": "send", "description": "Send a payload upstream."},
        ],
        "last_stage": None, "last_stage_ok": None,
        "created_at": 0, "updated_at": 0,
    }]
    from services import mcp_admin_service as admin
    monkeypatch.setattr(admin, "list_generated", lambda: list(fake))
    res = rpc.mcp_list_generated()
    rows = res["generated"]
    assert rows[0]["planned_tools"] == ["ping", "send"]
