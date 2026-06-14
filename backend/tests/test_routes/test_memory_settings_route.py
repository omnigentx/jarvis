"""HTTP round-trip for /api/memory/settings (GET defaults, PATCH, validation).

Real config_service against an isolated per-test DB; setup gate satisfied by
marking critical wizard steps complete. Fixture mirrors
test_context_compaction_routes.py.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "memory_routes.db"
    monkeypatch.setenv("JARVIS_DB_PATH", str(db_file))
    monkeypatch.setenv("JARVIS_API_KEY", "memory-routes-test-key")
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(tmp_path / "spawn_registry.db"))

    from core import auth as core_auth
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "memory-routes-test-key")
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
    # services.memory.settings imported config_service by value — repoint it too.
    from services.memory import settings as mem_settings
    monkeypatch.setattr(mem_settings, "config_service", config_module.config_service)

    from middleware.setup_gate import _reset_cache_for_tests, refresh_setup_complete
    _reset_cache_for_tests()
    refresh_setup_complete()

    from server import app
    yield TestClient(app, headers={"Authorization": "Bearer memory-routes-test-key"})
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None


def test_get_returns_defaults(client):
    r = client.get("/api/memory/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is False          # feature flag default OFF
    assert body["mode"] == "balanced"
    assert "curator_api_key" not in body      # secret never returned
    assert body["curator_api_key_set"] is False


def test_patch_updates_and_validates(client):
    r = client.patch("/api/memory/settings", json={"mode": "deep", "pinned_token_budget": 700})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "deep"
    assert r.json()["pinned_token_budget"] == 700

    bad = client.patch("/api/memory/settings", json={"mode": "turbo"})
    assert bad.status_code == 422


def test_index_status_route(client):
    r = client.get("/api/memory/index-status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "outbox" in body and "episodic_documents" in body


def test_memory_search_route_degraded(client):
    # No data + Qdrant absent → empty evidence, degraded flag, no crash.
    r = client.post("/api/agents/Jarvis/memory-search", json={"query": "compactor"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "evidence" in body and body["degraded"] is True


def test_list_memories_empty(client):
    r = client.get("/api/agents/Jarvis/memories")
    assert r.status_code == 200, r.text
    assert r.json() == {"total": 0, "items": []}


def _seed_memory():
    import core.database as cd
    from services.memory.memory_service import MemoryService
    db = cd.SessionLocal()
    try:
        rec = MemoryService(db).create_memory(
            owner_agent_name="Jarvis", memory_type="semantic",
            content="seeded decision about caching strategy",
            subject_scope="project:jarvis", authority="user_confirmed", now=100.0)
        return rec.id
    finally:
        db.close()


def test_archive_and_delete_routes(client):
    mid = _seed_memory()
    # listed as active
    assert client.get("/api/agents/Jarvis/memories").json()["total"] == 1
    # archive
    r = client.post(f"/api/agents/Jarvis/memories/{mid}/archive")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "archived"
    # cross-agent archive denied
    assert client.post(f"/api/agents/Riley/memories/{mid}/archive").status_code == 404
    # delete
    assert client.delete(f"/api/agents/Jarvis/memories/{mid}").status_code == 200


def test_candidate_approve_route(client):
    import core.database as cd
    from services.memory import candidate_service as cnd
    db = cd.SessionLocal()
    try:
        cand = cnd.create_candidate(
            db, owner_agent_name="Jarvis", candidate_type="fact",
            payload={"memory_type": "semantic", "content": "we picked Redis",
                     "subject_scope": "project:jarvis", "authority": "user_confirmed"},
            now=100.0, requires_approval=True)
        cid = cand.id
    finally:
        db.close()
    # pending in candidate list
    items = client.get("/api/agents/Jarvis/memory-candidates").json()["items"]
    assert any(c["id"] == cid for c in items)
    # approve → persists a memory
    r = client.post(f"/api/agents/Jarvis/memory-candidates/{cid}/approve")
    assert r.status_code == 200, r.text
    assert client.get("/api/agents/Jarvis/memories").json()["total"] >= 1
