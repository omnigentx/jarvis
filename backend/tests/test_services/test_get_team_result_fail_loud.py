"""Fail-loud contract for ``agent_spawner__get_team_result``.

When the orchestrator's ``spawn_registry.result`` is empty, the tool
MUST surface that fault via an ``error_state`` block so the calling
agent (Jarvis) can tell the user. Per project policy, no silent
fallback — the read path returns what it has, plus an explicit error
marker when the single source of truth is empty.

See ``services.context_persistence._update_spawn_registry_result``
for the write half of this contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ─────────────────────────────────────────────────────────


def _make_session(agents: dict, *, template_name: str = "agile-team", workspace="/tmp/ws"):
    """Build a TeamSession-like stub that ``get_team_result`` will accept."""
    session = MagicMock()
    session.agents = agents
    session.template = {"name": template_name}
    session.workspace = Path(workspace)
    return session


def _make_registry_record(status: str, result: str = ""):
    rec = MagicMock()
    rec.status = status
    rec.result = result
    return rec


# ── Tests ───────────────────────────────────────────────────────────


def test_get_team_result_emits_error_state_when_orchestrator_result_empty():
    """Empty result for the PM/orchestrator → ``error_state`` block
    names the agent, role, and points at the fix location."""
    from fast_agent.spawn.servers import agent_spawner_server as srv

    agents = {
        "Morgan [PM]": {
            "run_id": "run-pm", "role": "pm", "status": "idle",
            "result": "", "agent_name": "Morgan [PM]",
        },
        "Ryan [BA]": {
            "run_id": "run-ba", "role": "ba", "status": "idle",
            "result": "Ryan's audit summary", "agent_name": "Ryan [BA]",
        },
    }
    session = _make_session(agents)

    fake_registry = MagicMock()
    fake_registry.get_latest.side_effect = lambda rid: {
        "run-pm": _make_registry_record("idle", ""),
        "run-ba": _make_registry_record("idle", "Ryan's audit summary"),
    }.get(rid)

    with patch.object(srv, "get_team_session", return_value=session), \
            patch.object(srv, "_registry", fake_registry), \
            patch.object(srv, "get_workspace_summary",
                         return_value={"directories": {"specs": [], "src": []}}):
        payload = json.loads(srv.get_team_result("session-1"))

    assert "error_state" in payload, "fail-loud marker missing"
    es = payload["error_state"]
    assert es["code"] == "orchestrator_result_missing"
    assert es["orchestrator"] == "Morgan [PM]"
    assert "pm" in es["role"]
    # detail must point a future reader at the proper write hook
    assert "save_agent_context" in es["detail"]
    assert "agent_context_snapshots" in es["detail"]

    # Other agents' results still come through unchanged
    assert payload["agents"]["Ryan [BA]"]["result"] == "Ryan's audit summary"


def test_get_team_result_omits_error_state_when_orchestrator_result_present():
    """Happy path: PM has a real roll-up → no error_state, payload is
    the same shape it was before this change."""
    from fast_agent.spawn.servers import agent_spawner_server as srv

    rollup = "# Self-audit — Team Roll-up\nVerdict: PARTIAL"
    agents = {
        "Morgan [PM]": {
            "run_id": "run-pm", "role": "pm", "status": "idle",
            "result": rollup, "agent_name": "Morgan [PM]",
        },
    }
    session = _make_session(agents)

    fake_registry = MagicMock()
    fake_registry.get_latest.return_value = _make_registry_record("idle", rollup)

    with patch.object(srv, "get_team_session", return_value=session), \
            patch.object(srv, "_registry", fake_registry), \
            patch.object(srv, "get_workspace_summary",
                         return_value={"directories": {}}):
        payload = json.loads(srv.get_team_result("session-2"))

    assert "error_state" not in payload
    assert payload["agents"]["Morgan [PM]"]["result"].startswith("# Self-audit")


def test_get_team_result_falls_back_to_first_agent_when_no_pm_role():
    """Some templates don't have an explicit ``pm`` role — the
    notification path picks the first spawned agent in that case, and
    so must ``get_team_result`` so both surfaces always point at the
    same "orchestrator" when surfacing the fault."""
    from fast_agent.spawn.servers import agent_spawner_server as srv

    agents = {
        "Lead [Coord]": {
            "run_id": "run-lead", "role": "coordinator", "status": "idle",
            "result": "", "agent_name": "Lead [Coord]", "started_at": 1.0,
        },
        "Helper [Worker]": {
            "run_id": "run-worker", "role": "worker", "status": "idle",
            "result": "did stuff", "agent_name": "Helper [Worker]",
            "started_at": 2.0,
        },
    }
    session = _make_session(agents, template_name="no-pm-team")

    fake_registry = MagicMock()
    fake_registry.get_latest.side_effect = lambda rid: {
        "run-lead": _make_registry_record("idle", ""),
        "run-worker": _make_registry_record("idle", "did stuff"),
    }.get(rid)

    with patch.object(srv, "get_team_session", return_value=session), \
            patch.object(srv, "_registry", fake_registry), \
            patch.object(srv, "get_workspace_summary",
                         return_value={"directories": {}}):
        payload = json.loads(srv.get_team_result("session-3"))

    assert "error_state" in payload
    # First spawned agent is the "orchestrator" surrogate when no PM exists.
    assert payload["error_state"]["orchestrator"] == "Lead [Coord]"


def test_get_team_result_session_not_found_returns_error():
    """Existing behaviour preserved: missing session → ``error`` field."""
    from fast_agent.spawn.servers import agent_spawner_server as srv

    with patch.object(srv, "get_team_session", return_value=None):
        payload = json.loads(srv.get_team_result("does-not-exist"))

    assert "error" in payload
    assert "does-not-exist" in payload["error"]
    # No error_state on top of error — the session lookup short-circuits earlier.
    assert "error_state" not in payload
