"""Tests for tools.skill_server — MCP tools that delegate to the
RuntimeRpcServer in the main backend process.

The tools themselves are thin shims (see ``tools/skill_server.py``).
What we want to verify here:

  - The experimental flag gate runs BEFORE any RPC call
  - The right method/params get sent to the bridge
  - Backend errors are forwarded as structured ``{"error", "status"}`` envelopes
  - Transport-level failures (no socket, dead socket) become a clear 503

Side-effect checks (notification rows) live in test_skill_rpc_handlers.py
because that's where the row is actually written — the subprocess no
longer touches the DB.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools import skill_server as srv
from tools import runtime_rpc_client as rpc_mod


# ----- Fixtures ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_self_improving(monkeypatch):
    monkeypatch.setattr(srv, "_is_enabled", lambda: True)


@pytest.fixture()
def rpc_calls(monkeypatch):
    """Record RPC calls and feed back canned responses keyed on method name."""
    calls: list[dict] = []
    responses: dict[str, dict] = {}

    def fake_call(method, params=None, *, timeout=30.0):
        calls.append({"method": method, "params": params or {}})
        return responses.get(method, {"error": "no-fixture", "status": -1})

    monkeypatch.setattr(srv, "rpc_call", fake_call)
    return {"calls": calls, "responses": responses}


# ----- Read tools --------------------------------------------------------


class TestReadTools:
    def test_skill_list_calls_skill_dot_list(self, rpc_calls):
        rpc_calls["responses"]["skill.list"] = {"skills": []}
        assert srv.skill_list() == {"skills": []}
        assert rpc_calls["calls"] == [{"method": "skill.list", "params": {}}]

    def test_skill_get_passes_name(self, rpc_calls):
        rpc_calls["responses"]["skill.get"] = {"name": "demo"}
        assert srv.skill_get("demo") == {"name": "demo"}
        assert rpc_calls["calls"][0]["params"] == {"name": "demo"}

    def test_backend_error_is_forwarded(self, rpc_calls):
        rpc_calls["responses"]["skill.get"] = {"error": "Skill 'x' not found.", "status": 404}
        result = srv.skill_get("x")
        assert result["status"] == 404


# ----- Mutating tools ----------------------------------------------------


class TestMutatingTools:
    def test_create_passes_name_description_body(self, rpc_calls):
        rpc_calls["responses"]["skill.create"] = {"created": True, "name": "x", "is_builtin": False}
        srv.skill_create(name="x", description="d", body="b")
        assert rpc_calls["calls"][0] == {
            "method": "skill.create",
            "params": {"name": "x", "description": "d", "body": "b"},
        }

    def test_update_passes_name_and_content(self, rpc_calls):
        rpc_calls["responses"]["skill.update"] = {"updated": True, "name": "demo"}
        srv.skill_update("demo", "---\nname: demo\ndescription: x\n---\nbody\n")
        sent = rpc_calls["calls"][0]
        assert sent["method"] == "skill.update"
        assert sent["params"]["name"] == "demo"
        assert "demo" in sent["params"]["content"]

    def test_delete_passes_name(self, rpc_calls):
        rpc_calls["responses"]["skill.delete"] = {"deleted": True, "name": "x", "removed_from_agents": []}
        srv.skill_delete("x")
        assert rpc_calls["calls"][0]["params"] == {"name": "x"}

    def test_attach_passes_skill_and_agent(self, rpc_calls):
        rpc_calls["responses"]["skill.attach"] = {
            "agent": "Jarvis", "skill": "s", "persisted": False, "skills": [],
        }
        srv.skill_attach(skill="s", agent="Jarvis")
        assert rpc_calls["calls"][0]["params"] == {"skill": "s", "agent": "Jarvis"}

    def test_detach_passes_skill_and_agent(self, rpc_calls):
        rpc_calls["responses"]["skill.detach"] = {
            "agent": "A", "skill": "s", "persisted": True, "skills": [],
        }
        srv.skill_detach(skill="s", agent="A")
        assert rpc_calls["calls"][0]["params"] == {"skill": "s", "agent": "A"}


# ----- Hot-reload gate ---------------------------------------------------


class TestExperimentalGate:
    def test_disabled_blocks_every_tool_without_rpc(self, rpc_calls, monkeypatch):
        monkeypatch.setattr(srv, "_is_enabled", lambda: False)

        cases = [
            (srv.skill_list, ()),
            (srv.skill_get, ("demo",)),
            (srv.skill_create, ("new", "d", "b")),
            (srv.skill_update, ("demo", "x")),
            (srv.skill_delete, ("demo",)),
            (srv.skill_attach, ("demo", "Jarvis")),
            (srv.skill_detach, ("demo", "Jarvis")),
        ]
        for fn, args in cases:
            result = fn(*args)
            assert result["status"] == 503, f"{fn.__name__} returned {result}"
            assert "disabled" in result["error"].lower()

        # Critical: gate ran first → no RPC was attempted.
        assert rpc_calls["calls"] == []


# ----- Transport failure -------------------------------------------------


class TestBridgeUnavailable:
    def test_missing_socket_envelopes_to_503(self, monkeypatch):
        def boom(method, params=None, *, timeout=30.0):
            raise rpc_mod.RuntimeRpcError("JARVIS_RUNTIME_RPC_SOCKET not set")
        monkeypatch.setattr(srv, "rpc_call", boom)
        result = srv.skill_list()
        assert result["status"] == 503
        assert "RPC bridge" in result["error"]
