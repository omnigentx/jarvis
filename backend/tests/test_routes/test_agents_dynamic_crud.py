"""Integration tests for the dynamic-agent CRUD endpoints.

Exercises POST/PUT/DELETE /api/agents against a real SQLite database
(not a mock). The cross-layer invariant under test is:

    HTTP request -> agent_definitions DB row -> rev counter bump

so that the parent process's poll loop will pick up the change. We do
NOT exercise the poll loop or `agent_app.load_agent_data` here — those
have their own tests (`test_dynamic_agents.py` and the submodule
`test_load_agent_data.py`). What we DO assert is that the contract
between the API and the storage layer is intact: the route writes the
row, validation runs, and the rev counter advances exactly once per
successful mutation. Without these tests, a refactor that swapped DB
writes for a no-op would still pass the unit tests above — bug class
guarded against: "endpoint thinks it persisted; DB is empty".
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth as core_auth

_KEY = "agents-crud-tests-master-key"
AUTH = {"Authorization": f"Bearer {_KEY}"}


@pytest.fixture()
def db_and_client(tmp_path, monkeypatch):
    """Wire a TestClient against the real /api/agents router with an
    isolated SQLite DB. Patch fast.agents / _agent_card_sources to be
    empty so `_is_static_agent` never blocks the test agent names.
    """
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(tmp_path / "test_dyn_crud.db"))
    monkeypatch.setenv("JARVIS_API_KEY", _KEY)
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", _KEY)

    from routes import agents as agents_route

    # Empty fast.agents → no static agent names to collide with.
    fake_fast = MagicMock()
    fake_fast.agents = {}
    fake_fast._agent_card_sources = {}
    monkeypatch.setattr(agents_route, "fast", fake_fast)
    monkeypatch.setattr(agents_route, "activity_stream_manager", MagicMock())

    app = FastAPI()
    app.include_router(agents_route.router)
    return TestClient(app)


# ── POST /api/agents ──────────────────────────────────────────────────


def test_create_persists_row_and_bumps_rev(db_and_client):
    from services import agent_definitions as defs_svc

    rev_before = defs_svc.get_rev()
    r = db_and_client.post(
        "/api/agents",
        json={
            "name": "Researcher",
            "instruction": "Find things.",
            "servers": ["serpapi"],
            "use_history": True,
        },
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "created", "name": "Researcher"}

    row = defs_svc.get_definition("Researcher")
    assert row is not None
    assert row["instruction"] == "Find things."
    assert row["servers"] == ["serpapi"]
    assert defs_svc.get_rev() == rev_before + 1


def test_create_duplicate_returns_409(db_and_client):
    from services import agent_definitions as defs_svc

    defs_svc.create_definition(name="Dup", instruction="first")
    r = db_and_client.post(
        "/api/agents",
        json={"name": "Dup", "instruction": "second"},
        headers=AUTH,
    )
    assert r.status_code == 409


def test_create_invalid_name_returns_400(db_and_client):
    r = db_and_client.post(
        "/api/agents",
        json={"name": "123bad", "instruction": "x"},
        headers=AUTH,
    )
    assert r.status_code == 400


def test_create_empty_model_stored_as_null(db_and_client):
    """Wire payload `model: ""` means "use default" — DB should hold NULL,
    not an empty string. Without normalisation, the load pipeline would
    carry an empty-string sentinel through to RequestParams.model."""
    from services import agent_definitions as defs_svc

    r = db_and_client.post(
        "/api/agents",
        json={"name": "DefaultModel", "instruction": "x", "model": ""},
        headers=AUTH,
    )
    assert r.status_code == 200

    row = defs_svc.get_definition("DefaultModel")
    assert row["model"] is None


# ── PUT /api/agents/{name} ────────────────────────────────────────────


def test_update_partial_writes_and_bumps_rev(db_and_client):
    from services import agent_definitions as defs_svc

    defs_svc.create_definition(name="U", instruction="orig", servers=["s1"])
    rev_before = defs_svc.get_rev()

    r = db_and_client.put(
        "/api/agents/U",
        json={"instruction": "new"},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text

    row = defs_svc.get_definition("U")
    assert row["instruction"] == "new"
    assert row["servers"] == ["s1"]  # untouched
    assert defs_svc.get_rev() == rev_before + 1


def test_update_missing_returns_404(db_and_client):
    r = db_and_client.put(
        "/api/agents/Ghost",
        json={"instruction": "x"},
        headers=AUTH,
    )
    assert r.status_code == 404


def test_update_empty_body_returns_400(db_and_client):
    from services import agent_definitions as defs_svc

    defs_svc.create_definition(name="E", instruction="x")
    r = db_and_client.put(
        "/api/agents/E",
        json={},
        headers=AUTH,
    )
    assert r.status_code == 400


# ── DELETE /api/agents/{name} ─────────────────────────────────────────


def test_delete_removes_row_and_bumps_rev(db_and_client):
    from services import agent_definitions as defs_svc

    defs_svc.create_definition(name="D", instruction="d")
    rev_before = defs_svc.get_rev()

    r = db_and_client.delete("/api/agents/D", headers=AUTH)
    assert r.status_code == 200, r.text
    assert defs_svc.get_definition("D") is None
    assert defs_svc.get_rev() == rev_before + 1


def test_delete_missing_falls_through_to_spawn_registry(db_and_client, monkeypatch):
    """When the name is not a DB definition and not in spawn registry,
    the endpoint must surface 404, not silently return success."""
    monkeypatch.setattr("services.shared_state.registry_db", None, raising=False)
    r = db_and_client.delete("/api/agents/Nope", headers=AUTH)
    assert r.status_code == 404


# ── Static-agent guard rails ─────────────────────────────────────────


def test_create_blocked_when_name_is_static(db_and_client, monkeypatch):
    """Code-defined agents (decorators in agent.py) cannot be overridden
    via the API. The endpoint detects this via `name in fast.agents`
    and `name not in fast._agent_card_sources`."""
    from routes import agents as agents_route

    agents_route.fast.agents = {"PersonalAgent": {"config": object()}}
    agents_route.fast._agent_card_sources = {}

    r = db_and_client.post(
        "/api/agents",
        json={"name": "PersonalAgent", "instruction": "x"},
        headers=AUTH,
    )
    assert r.status_code == 409


def test_update_blocked_when_name_is_static(db_and_client):
    from routes import agents as agents_route

    agents_route.fast.agents = {"PersonalAgent": {"config": object()}}
    agents_route.fast._agent_card_sources = {}

    r = db_and_client.put(
        "/api/agents/PersonalAgent",
        json={"instruction": "x"},
        headers=AUTH,
    )
    assert r.status_code == 403


def test_delete_blocked_when_name_is_static(db_and_client):
    from routes import agents as agents_route

    agents_route.fast.agents = {"PersonalAgent": {"config": object()}}
    agents_route.fast._agent_card_sources = {}

    r = db_and_client.delete("/api/agents/PersonalAgent", headers=AUTH)
    assert r.status_code == 403


# ── Full lifecycle round-trip ────────────────────────────────────────


def test_full_lifecycle_rev_advances_exactly_once_per_mutation(db_and_client):
    """Catches any path that double-bumps the rev counter (which would
    cause the reload loop to fire twice for one logical change) or
    fails to bump on a successful mutation (loop misses the change)."""
    from services import agent_definitions as defs_svc

    rev0 = defs_svc.get_rev()
    # create
    db_and_client.post(
        "/api/agents",
        json={"name": "Lifecycle", "instruction": "v1"},
        headers=AUTH,
    )
    rev1 = defs_svc.get_rev()
    assert rev1 == rev0 + 1

    # update
    db_and_client.put(
        "/api/agents/Lifecycle",
        json={"instruction": "v2"},
        headers=AUTH,
    )
    rev2 = defs_svc.get_rev()
    assert rev2 == rev1 + 1

    # update with no changes still bumps (PUT is not idempotent on
    # updated_at + rev — that's intentional; the loop's polling cost
    # is one DB read, far cheaper than diffing every column for true
    # idempotency).
    db_and_client.put(
        "/api/agents/Lifecycle",
        json={"instruction": "v2"},  # same value
        headers=AUTH,
    )
    rev3 = defs_svc.get_rev()
    assert rev3 == rev2 + 1

    # delete
    db_and_client.delete("/api/agents/Lifecycle", headers=AUTH)
    rev4 = defs_svc.get_rev()
    assert rev4 == rev3 + 1
