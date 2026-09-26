"""Behavioral tests for live team model changes and isolation."""
import asyncio
import json
import sqlite3

import pytest

from services.team_model_runtime import (
    ModelChangeConflict,
    ModelChangeError,
    ModelChangeForbidden,
    change_model,
    create_model_hook,
    snapshot,
)
from services.model_rpc_handlers import _get as rpc_get
from fast_agent.config import OpenAISettings, Settings
from fast_agent.context import Context
from fast_agent.llm.provider.openai.llm_openai import OpenAILLM


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.db"
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(path))
    monkeypatch.setattr(
        "services.model_catalog.available_models",
        lambda: frozenset({"coding-agent", "gpt-4o-mini"}),
    )
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE spawn_registry (run_id TEXT PRIMARY KEY, data_json TEXT NOT NULL)")
        conn.execute("CREATE TABLE team_sessions (session_id TEXT PRIMARY KEY, data_json TEXT NOT NULL)")
        roles = {role: {"capabilities": ["model-management"]} for role in ("pm", "dev", "qe")}
        for session in ("team-a", "team-b"):
            conn.execute("INSERT INTO team_sessions VALUES (?, ?)", (
                session, json.dumps({"template": {"orchestrator": "pm", "roles": roles}}),
            ))
        for run_id, name, role, session in (
            ("pm-a", "Peyton [PM]", "pm", "team-a"),
            ("pm-a-resumed", "Peyton [PM]", "pm", "team-a"),
            ("dev-a", "Emerson [Dev]", "dev", "team-a"),
            ("qe-a", "Morgan [QE]", "qe", "team-a"),
            ("dev-b", "Taylor [Dev]", "dev", "team-b"),
        ):
            conn.execute("INSERT INTO spawn_registry VALUES (?, ?)", (
                run_id,
                json.dumps({"agent_name": name, "role": role, "session_id": session,
                            "original_config": {"model": "openai.coding-agent"}}),
            ))
    return path


def test_pm_can_change_member_and_revision_is_atomic(registry):
    result = change_model("dev-a", "openai.gpt-4o-mini", 0,
                          actor="agent:Peyton [PM]", caller_agent="Peyton [PM]")
    assert result["model_revision"] == 1
    assert snapshot("dev-a")["model"] == "openai.gpt-4o-mini"
    with pytest.raises(ModelChangeConflict):
        change_model("dev-a", "openai.coding-agent", 0, actor="user")
    with sqlite3.connect(registry) as conn:
        assert conn.execute(
            "SELECT outcome FROM team_model_change_audit ORDER BY id"
        ).fetchall() == [("success",), ("conflict",)]


def test_resumed_run_keeps_team_model_without_cross_team_leak(registry):
    change_model("dev-a", "openai.gpt-4o-mini", 0, actor="user")
    with sqlite3.connect(registry) as conn:
        conn.execute("INSERT INTO spawn_registry VALUES (?, ?)", (
            "dev-a-resumed",
            json.dumps({"agent_name": "Emerson [Dev]", "role": "dev", "session_id": "team-a",
                        "original_config": {"model": "openai.coding-agent"}}),
        ))
    assert snapshot("dev-a-resumed")["model"] == "openai.gpt-4o-mini"
    assert snapshot("dev-b")["model"] == "openai.coding-agent"


def test_member_self_change_but_not_peer_or_other_team(registry):
    result = change_model("dev-a", "openai.gpt-4o-mini", 0,
                          actor="agent:Emerson", caller_agent="Emerson [Dev]")
    assert result["changed"]
    with pytest.raises(ModelChangeForbidden):
        change_model("qe-a", "openai.gpt-4o-mini", 0,
                     actor="agent:Emerson", caller_agent="Emerson [Dev]")
    with pytest.raises(ModelChangeForbidden):
        change_model("dev-b", "openai.gpt-4o-mini", 0,
                     actor="agent:Peyton", caller_agent="Peyton [PM]")
    with pytest.raises(ModelChangeForbidden):
        snapshot("dev-b", caller_agent="Peyton [PM]")


def test_invalid_provider_rejected_without_write(registry):
    with pytest.raises(ModelChangeError):
        change_model("dev-a", "anthropic.claude", 0, actor="user")
    assert snapshot("dev-a")["model_revision"] == 0


def test_unknown_model_rejected_without_mutation(registry):
    with pytest.raises(ModelChangeError, match="unavailable"):
        change_model("dev-a", "openai.not-real", 0, actor="user")
    assert snapshot("dev-a")["model_revision"] == 0


def test_capability_is_required_even_for_self(registry):
    with sqlite3.connect(registry) as conn:
        conn.execute("UPDATE team_sessions SET data_json = ? WHERE session_id = ?", (
            json.dumps({"template": {"orchestrator": "pm", "roles": {
                "pm": {"capabilities": ["model-management"]}, "dev": {"capabilities": []},
            }}}), "team-a",
        ))
    with pytest.raises(ModelChangeForbidden):
        change_model("dev-a", "openai.gpt-4o-mini", 0,
                     actor="agent:Emerson", caller_agent="Emerson [Dev]")
    assert snapshot("dev-a")["model_revision"] == 0


def test_rpc_resolves_logical_agent_in_bound_session(registry):
    result = rpc_get("Emerson [Dev]", "team-a")
    assert result["run_id"] == "dev-a"
    with pytest.raises(ValueError, match="no run"):
        rpc_get("Emerson [Dev]", "team-b", "Morgan [QE]")
    with pytest.raises(PermissionError):
        rpc_get("Emerson [Dev]", "")


def test_hook_reads_new_model_on_next_call(registry):
    class Runner:
        def __init__(self):
            self.request_params = None

        def set_request_params(self, params):
            self.request_params = params

    runner = Runner()
    hook = create_model_hook("dev-a")
    asyncio.run(hook.before_llm_call(runner, []))
    assert runner.request_params is None
    change_model("dev-a", "openai.gpt-4o-mini", 0, actor="user")
    asyncio.run(hook.before_llm_call(runner, []))
    assert runner.request_params.model == "gpt-4o-mini"
    change_model("dev-a", "openai.coding-agent", 1, actor="user")
    asyncio.run(hook.before_llm_call(runner, []))
    assert runner.request_params.model == "coding-agent"


def test_hook_changes_actual_openai_request_model(registry):
    """The provider request body, not just the hook event, must change."""
    class Runner:
        request_params = None

        def set_request_params(self, params):
            self.request_params = params

    provider = OpenAILLM(
        context=Context(config=Settings(openai=OpenAISettings(default_model="coding-agent"))),
        model="coding-agent",
    )
    runner = Runner()
    hook = create_model_hook("dev-a")
    asyncio.run(hook.before_llm_call(runner, []))
    request_before = provider._prepare_api_request([], None, runner.request_params or provider.default_request_params)
    change_model("dev-a", "openai.gpt-4o-mini", 0, actor="user")
    asyncio.run(hook.before_llm_call(runner, []))
    request_after = provider._prepare_api_request([], None, runner.request_params)
    assert request_before["model"] == "coding-agent"
    assert request_after["model"] == "gpt-4o-mini"


def test_active_snapshot_stays_old_until_next_call(registry):
    class Runner:
        request_params = None

        def set_request_params(self, params):
            self.request_params = params

    runner = Runner()
    hook = create_model_hook("dev-a")
    asyncio.run(hook.before_llm_call(runner, []))
    assert snapshot("dev-a")["active_model"] == "openai.coding-agent"
    change_model("dev-a", "openai.gpt-4o-mini", 0, actor="user")
    state = snapshot("dev-a")
    assert state["configured_model"] == "openai.gpt-4o-mini"
    assert state["active_model"] == "openai.coding-agent"
    asyncio.run(hook.after_llm_call(runner, None))
    assert snapshot("dev-a")["active_model"] is None
    asyncio.run(hook.before_llm_call(runner, []))
    assert snapshot("dev-a")["active_model"] == "openai.gpt-4o-mini"
