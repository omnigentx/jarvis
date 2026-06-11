"""Route tests for context-compaction settings + version endpoints.

Settings go through the REAL config_service (isolated per-test DB);
versions go through the REAL sqlite snapshot DB (tmp via
SPAWN_REGISTRY_DB) — no mocking of the layers under test. Fixture
pattern mirrors tests/test_routes/test_voice_routes.py.
"""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from services.context_compaction import DEFAULTS
from services.context_persistence import save_compaction_event


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "compaction_routes.db"
    monkeypatch.setenv("JARVIS_DB_PATH", str(db_file))
    monkeypatch.setenv("JARVIS_API_KEY", "compaction-routes-test-key")
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(tmp_path / "spawn_registry.db"))

    from core import auth as core_auth
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "compaction-routes-test-key")
    from core import secrets_crypto
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None

    from sqlalchemy import create_engine as _ce
    from sqlalchemy.orm import sessionmaker
    from core.database import Base, SETUP_WIZARD_CRITICAL_STEPS, SetupWizardStep
    eng = _ce(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    with SessionFactory() as db:
        for name in SETUP_WIZARD_CRITICAL_STEPS:
            db.add(SetupWizardStep(step_name=name, completed=True))
        db.commit()

    import core.database as core_db
    from services import config_service as config_module
    monkeypatch.setattr(core_db, "SessionLocal", SessionFactory)
    monkeypatch.setattr(config_module, "SessionLocal", SessionFactory)
    monkeypatch.setattr(
        config_module, "config_service", config_module.ConfigService(db_factory=SessionFactory)
    )

    from middleware.setup_gate import _reset_cache_for_tests, refresh_setup_complete
    _reset_cache_for_tests()
    refresh_setup_complete()

    from server import app
    yield TestClient(app, headers={"Authorization": "Bearer compaction-routes-test-key"})
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None


# ── Settings ──


def test_get_settings_returns_defaults(client):
    resp = client.get("/api/context-compaction/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["snapshot_versions_visible"] == 3
    assert body["compact_at_ratio"] == 0.7
    assert set(body) == set(DEFAULTS)


def test_patch_settings_persists_and_returns_merged(client):
    resp = client.patch(
        "/api/context-compaction/settings",
        json={"compact_at_ratio": 0.8, "snapshot_versions_visible": 5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["compact_at_ratio"] == 0.8
    assert body["snapshot_versions_visible"] == 5
    # Persisted — a fresh GET (new config read) agrees.
    again = client.get("/api/context-compaction/settings").json()
    assert again["compact_at_ratio"] == 0.8
    # Untouched keys keep their defaults.
    assert again["keep_recent_messages"] == DEFAULTS["keep_recent_messages"]


def test_patch_settings_rejects_out_of_range(client):
    resp = client.patch(
        "/api/context-compaction/settings", json={"compact_at_ratio": 0.1}
    )
    assert resp.status_code == 422
    assert "compact_at_ratio" in resp.json()["detail"]


def test_patch_settings_rejects_empty(client):
    resp = client.patch("/api/context-compaction/settings", json={})
    assert resp.status_code == 422


def test_settings_require_auth(client):
    resp = client.get(
        "/api/context-compaction/settings", headers={"Authorization": ""}
    )
    assert resp.status_code == 401


# ── Versions ──


def test_versions_list_and_detail(client):
    for i in range(5):
        save_compaction_event(
            agent_name="Jarvis", run_id=f"r{i}", raw_snapshot_id=100 + i,
            working_context_json='{"messages": []}',
            summary_message=f"[COMPACTED_CONTEXT_SUMMARY] v{i}",
            plan_json='{"delete_from_working_context": [2, 3]}',
            message_count_before=20, message_count_after=8,
            estimated_tokens_before=10000, estimated_tokens_after=4000,
            confidence=0.6, status="completed",
        )

    # Default limit comes from snapshot_versions_visible (3).
    resp = client.get("/api/agents/Jarvis/context/versions")
    assert resp.status_code == 200, resp.text
    versions = resp.json()["versions"]
    assert len(versions) == 3
    v = versions[0]
    assert v["saved_tokens"] == 6000
    assert v["reduction_ratio"] == 0.6
    assert "working_context_json" not in v  # metadata-first

    # Explicit limit wins.
    resp = client.get("/api/agents/Jarvis/context/versions?limit=5")
    assert len(resp.json()["versions"]) == 5

    # Detail includes summary + plan, scoped by agent name.
    detail = client.get(f"/api/agents/Jarvis/context/versions/{v['id']}").json()
    assert detail["summary_message"].startswith("[COMPACTED_CONTEXT_SUMMARY]")
    assert detail["plan"]["delete_from_working_context"] == [2, 3]
    other = client.get(f"/api/agents/Other/context/versions/{v['id']}")
    assert other.status_code == 404


def test_versions_limit_follows_settings_change(client):
    for i in range(8):
        save_compaction_event(agent_name="Jarvis", status="completed",
                              working_context_json='{"messages": []}')
    client.patch(
        "/api/context-compaction/settings", json={"snapshot_versions_visible": 6}
    )
    resp = client.get("/api/agents/Jarvis/context/versions")
    assert len(resp.json()["versions"]) == 6


def test_versions_empty_state(client):
    resp = client.get("/api/agents/Nobody/context/versions")
    assert resp.status_code == 200
    assert resp.json()["versions"] == []


def test_diff_endpoint(client, tmp_path):
    # Raw snapshot the diff reads "before" from.
    reg_db = tmp_path / "spawn_registry.db"
    conn = sqlite3.connect(reg_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_context_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            agent_name TEXT NOT NULL, session_id TEXT, team_name TEXT,
            context_json TEXT NOT NULL, message_count INTEGER DEFAULT 0,
            total_input_tokens INTEGER DEFAULT 0, total_output_tokens INTEGER DEFAULT 0,
            trigger TEXT DEFAULT 'manual', created_at REAL NOT NULL)
    """)
    raw_json = (
        '{"messages": ['
        '{"role": "user", "content": [{"type": "text", "text": "hello"}]},'
        '{"role": "assistant", "content": [{"type": "text", "text": "old turn"}]},'
        '{"role": "assistant", "content": [{"type": "text", "text": "recent"}]}'
        ']}'
    )
    cur = conn.execute(
        "INSERT INTO agent_context_snapshots (run_id, agent_name, context_json, created_at) "
        "VALUES ('r', 'Jarvis', ?, ?)",
        (raw_json, time.time()),
    )
    raw_id = cur.lastrowid
    conn.commit()
    conn.close()

    working_json = (
        '{"messages": ['
        '{"role": "user", "content": [{"type": "text", "text": "hello"}]},'
        '{"role": "user", "content": [{"type": "text", "text": "[COMPACTED_CONTEXT_SUMMARY]\\n..."}]},'
        '{"role": "assistant", "content": [{"type": "text", "text": "recent"}]}'
        ']}'
    )
    event_id = save_compaction_event(
        agent_name="Jarvis", raw_snapshot_id=raw_id,
        working_context_json=working_json,
        plan_json='{"delete_from_working_context": [1], "summarize": []}',
        status="completed",
    )

    resp = client.get(f"/api/agents/Jarvis/context/versions/{event_id}/diff")
    assert resp.status_code == 200, resp.text
    diff = resp.json()
    assert [m["disposition"] for m in diff["before"]] == ["kept", "dropped", "kept"]
    assert [m["disposition"] for m in diff["after"]] == ["kept", "summary", "kept"]

    # Failed events have no diff.
    failed_id = save_compaction_event(
        agent_name="Jarvis", status="failed", error_message="nope",
    )
    resp = client.get(f"/api/agents/Jarvis/context/versions/{failed_id}/diff")
    assert resp.status_code == 404
