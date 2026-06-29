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
    # The dense block is the core observable this surfaces: the backend is
    # LadybugDB and `embeddings` reports whether dense recall is actually live
    # (the missing-FlagEmbedding incident showed as embeddings=False).
    assert body["dense"]["backend"] == "ladybug"
    assert "embeddings" in body["dense"] and "reachable" in body["dense"]


def test_memory_search_route_degraded(client):
    # No data + dense lane absent → empty evidence, degraded flag, no crash.
    r = client.post("/api/agents/Jarvis/memory-search", json={"query": "compactor"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "evidence" in body and body["degraded"] is True


def test_memory_graph_route_empty_when_store_unavailable(client, monkeypatch):
    # Memory ENABLED but the LadybugDB store can't be opened → the route returns
    # an empty, available=False payload (never 500s) so the UI shows an empty
    # state. (Forces the store path, not the disabled short-circuit.)
    import types

    import routes.memory as rm
    monkeypatch.setattr(rm, "get_memory_settings",
                        lambda: types.SimpleNamespace(enabled=True, ladybug_path="unused"))

    def _no_store(*_a, **_k):
        raise RuntimeError("store unavailable")
    monkeypatch.setattr("services.indexing.ladybug_store.get_ladybug_store", _no_store)
    r = client.get("/api/agents/Jarvis/memory-graph")
    assert r.status_code == 200, r.text
    assert r.json() == {"nodes": [], "edges": [], "available": False}


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
            subject_scope="project:jarvis", authority="user_confirmed", confidence=0.5, now=100.0)
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


def test_patch_warms_on_reranker_enable_and_revision_change(client, monkeypatch):
    """Pre-warm must fire on enabling the reranker (payload carries only
    reranker_enabled) and on an embedding_revision-only change — both alter which
    model loads, the cold-load this feature targets (PR #114 review)."""
    import routes.memory_settings as rms
    rr, emb = [], []
    monkeypatch.setattr(rms, "_kick_reranker_warm", lambda m: rr.append(m))
    monkeypatch.setattr(rms, "_kick_embedding_warm", lambda m, rev: emb.append((m, rev)))

    # Enabling the reranker → warm it (even though only reranker_enabled is sent).
    assert client.patch("/api/memory/settings", json={"reranker_enabled": True}).status_code == 200
    assert len(rr) == 1 and emb == []

    # A revision-only pin → warm the embedder (provider key is (model, revision)).
    rr.clear(); emb.clear()
    assert client.patch("/api/memory/settings", json={"embedding_revision": "rev-xyz"}).status_code == 200
    assert len(emb) == 1 and emb[0][1] == "rev-xyz" and rr == []

    # A non-model change → warm nothing.
    rr.clear(); emb.clear()
    assert client.patch("/api/memory/settings", json={"mode": "deep"}).status_code == 200
    assert rr == [] and emb == []
