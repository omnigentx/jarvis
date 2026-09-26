"""HTTP integration for authenticated team model changes and SSE dispatch."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from core import auth
    from routes import agents
    from services import shared_state

    path = tmp_path / "team-model.db"
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(path))
    monkeypatch.setenv("JARVIS_API_KEY", "model-api-test-key")
    monkeypatch.setattr(auth, "JARVIS_API_KEY", "model-api-test-key")
    monkeypatch.setattr(
        "services.model_catalog.available_models",
        lambda: frozenset({"coding-agent", "gpt-4o-mini"}),
    )
    record = {
        "run_id": "dev-run", "agent_name": "Sydney [Dev]", "role": "dev",
        "session_id": "team-api", "original_config": {"model": "openai.coding-agent"},
    }
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE spawn_registry (run_id TEXT PRIMARY KEY, data_json TEXT NOT NULL)")
        conn.execute("CREATE TABLE team_sessions (session_id TEXT PRIMARY KEY, data_json TEXT NOT NULL)")
        conn.execute("INSERT INTO spawn_registry VALUES (?, ?)", ("dev-run", json.dumps(record)))
        conn.execute("INSERT INTO team_sessions VALUES (?, ?)", (
            "team-api", json.dumps({"template": {"orchestrator": "pm", "roles": {
                "pm": {"capabilities": ["model-management"]},
                "dev": {"capabilities": ["model-management"]},
            }}}),
        ))
    monkeypatch.setattr(shared_state, "registry_db", SimpleNamespace(get_record=lambda run_id: record if run_id == "dev-run" else None))
    events = MagicMock()
    monkeypatch.setattr(agents, "activity_stream_manager", events)
    app = FastAPI()
    app.include_router(agents.router)
    return TestClient(app), path, events


def test_authenticated_change_updates_store_and_broadcasts_target(client):
    http, path, events = client
    endpoint = "/api/agents/Sydney%20%5BDev%5D/model"
    payload = {"run_id": "dev-run", "model_id": "openai.gpt-4o-mini", "expected_revision": 0}
    unauthorized = http.put(endpoint, json=payload)
    assert unauthorized.status_code in (401, 403)
    response = http.put(endpoint, json=payload, headers={"Authorization": "Bearer model-api-test-key"})
    assert response.status_code == 200, response.text
    assert response.json()["model_revision"] == 1
    event = events.broadcast.call_args.args[0]
    assert event["agent_name"] == "Sydney [Dev]"
    assert event["event_type"] == "model_changed"
    assert event["data"]["actor"] == "user:dashboard"
    assert event["data"]["previous_model"] == "openai.coding-agent"
    assert event["data"]["correlation_id"]
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT model, revision FROM team_model_configs WHERE session_id = ?",
            ("team-api",),
        ).fetchone() == ("openai.gpt-4o-mini", 1)
        assert conn.execute("SELECT outcome FROM team_model_change_audit").fetchall() == [("success",)]

    events.reset_mock()
    conflict = http.put(endpoint, json=payload, headers={"Authorization": "Bearer model-api-test-key"})
    assert conflict.status_code == 409
    events.broadcast.assert_not_called()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT outcome FROM team_model_change_audit ORDER BY id").fetchall() == [
            ("success",), ("conflict",),
        ]


def test_unknown_model_and_wrong_agent_do_not_change_store(client):
    http, path, events = client
    auth = {"Authorization": "Bearer model-api-test-key"}
    wrong = http.put("/api/agents/Other/model", json={
        "run_id": "dev-run", "model_id": "openai.gpt-4o-mini", "expected_revision": 0,
    }, headers=auth)
    assert wrong.status_code == 404
    unknown = http.put("/api/agents/Sydney%20%5BDev%5D/model", json={
        "run_id": "dev-run", "model_id": "openai.fake-model", "expected_revision": 0,
    }, headers=auth)
    assert unknown.status_code == 422
    events.broadcast.assert_not_called()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM team_model_configs").fetchone()[0] == 0
        assert conn.execute("SELECT outcome FROM team_model_change_audit").fetchall() == [("unavailable",)]
