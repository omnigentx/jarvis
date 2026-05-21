"""Tests for routes/team_template.py — REST surface for template edits.

End-to-end pipeline tests: HTTP → router → service → audit table. Mocks
the team_sessions store + team_reload module so we don't need a real
backend / fast-agent runtime. Pins the contract every UI / curl caller
will rely on.
"""
from __future__ import annotations

import copy
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import auth as core_auth
from core.database import Base


_API_KEY = "unit-test-master-key-team-template"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", _API_KEY)
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", _API_KEY)


@pytest.fixture()
def db_factory(tmp_path, monkeypatch):
    db_file = tmp_path / "team_template_test.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    import core.database as core_db
    monkeypatch.setattr(core_db, "SessionLocal", SessionFactory)
    yield SessionFactory
    engine.dispose()


@pytest.fixture()
def fake_store(monkeypatch):
    """In-memory replacement for the team_sessions store + get_team_session."""
    store_state = {
        "ses-1": {
            "session_id": "ses-1",
            "team_name": "test-team",
            "workspace": "/tmp/ws",
            "project_brief": "x",
            "parent_session_id": "",
            "conversation_id": "",
            "agents": {
                "QE-1": {"run_id": "run-old-qe", "role": "qe", "status": "idle"},
            },
            "sprint_status": "running",
            "template": {
                "name": "agile-team",
                "orchestrator": "pm",
                "roles": {
                    "qe": {
                        "role_display": "QE",
                        "instruction": "you are QE",
                        "servers": ["filesystem", "scrapling-server"],
                        "skills": ["qe-workflow"],
                        "model": "",
                        "server_overrides": {},
                    },
                    "dev": {
                        "role_display": "Dev",
                        "instruction": "you are dev",
                        "servers": ["filesystem", "git"],
                        "skills": ["dev-workflow"],
                        "model": "",
                        "server_overrides": {},
                    },
                },
            },
        },
    }

    from services import team_template_service as svc_mod

    def fake_get(sid):
        if sid not in store_state:
            raise svc_mod.NotFoundError(f"team session '{sid}' not found")
        return copy.deepcopy(store_state[sid])

    def fake_put(sid, data):
        store_state[sid] = copy.deepcopy(data)

    monkeypatch.setattr(svc_mod, "_get_team_session_dict", fake_get)
    monkeypatch.setattr(svc_mod, "_put_team_session_dict", fake_put)
    return store_state


@pytest.fixture()
def client(db_factory, fake_store):
    from routes.team_template import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _h() -> dict[str, str]:
    return {"Authorization": f"Bearer {_API_KEY}"}


# ── Auth gate ──────────────────────────────────────────────────────────────


class TestAuth:
    def test_get_requires_bearer(self, client):
        assert client.get("/api/team-sessions/ses-1/template").status_code == 401

    def test_patch_requires_bearer(self, client):
        r = client.patch(
            "/api/team-sessions/ses-1/template/roles/qe",
            json={"patch": {"servers": ["filesystem"]}},
        )
        assert r.status_code == 401


# ── GET template ───────────────────────────────────────────────────────────


class TestGetTemplate:
    def test_returns_current_state(self, client, fake_store):
        r = client.get("/api/team-sessions/ses-1/template", headers=_h())
        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == "ses-1"
        assert "qe" in body["template"]["roles"]
        assert body["template"]["roles"]["qe"]["servers"] == [
            "filesystem", "scrapling-server",
        ]

    def test_unknown_session_404(self, client):
        r = client.get("/api/team-sessions/nope/template", headers=_h())
        assert r.status_code == 404


# ── PATCH role ─────────────────────────────────────────────────────────────


class TestPatchRole:
    def test_add_server_writes_audit_and_persists(self, client, fake_store):
        r = client.patch(
            "/api/team-sessions/ses-1/template/roles/qe",
            json={
                "patch": {"servers": ["filesystem", "scrapling-server", "playwright"]},
                "comment": "add playwright for E2E",
            },
            headers=_h(),
        )
        assert r.status_code == 200
        body = r.json()
        assert "audit_ids" in body and len(body["audit_ids"]) == 1
        assert "warning" in body  # yaml-not-auto-synced warning
        assert "servers" in body["diff"]
        # State persisted
        assert "playwright" in fake_store["ses-1"]["template"]["roles"]["qe"]["servers"]

    def test_unknown_role_404(self, client):
        r = client.patch(
            "/api/team-sessions/ses-1/template/roles/unknown",
            json={"patch": {"servers": ["a"]}},
            headers=_h(),
        )
        assert r.status_code == 404

    def test_invalid_patch_400(self, client):
        r = client.patch(
            "/api/team-sessions/ses-1/template/roles/qe",
            json={"patch": {"cwd": "/etc"}},
            headers=_h(),
        )
        assert r.status_code == 400

    def test_noop_409(self, client):
        r = client.patch(
            "/api/team-sessions/ses-1/template/roles/qe",
            json={"patch": {"servers": ["scrapling-server", "filesystem"]}},
            headers=_h(),
        )
        assert r.status_code == 409


# ── History + rollback ─────────────────────────────────────────────────────


class TestHistoryAndRollback:
    def test_history_newest_first(self, client, fake_store):
        client.patch(
            "/api/team-sessions/ses-1/template/roles/qe",
            json={"patch": {"servers": ["a"]}}, headers=_h(),
        )
        client.patch(
            "/api/team-sessions/ses-1/template/roles/qe",
            json={"patch": {"servers": ["a", "b"]}}, headers=_h(),
        )
        r = client.get("/api/team-sessions/ses-1/template/history", headers=_h())
        body = r.json()
        assert body["count"] == 2
        assert body["rows"][0]["edited_at"] >= body["rows"][1]["edited_at"]

    def test_history_filter_role(self, client, fake_store):
        client.patch(
            "/api/team-sessions/ses-1/template/roles/qe",
            json={"patch": {"servers": ["a"]}}, headers=_h(),
        )
        client.patch(
            "/api/team-sessions/ses-1/template/roles/dev",
            json={"patch": {"servers": ["b"]}}, headers=_h(),
        )
        r = client.get(
            "/api/team-sessions/ses-1/template/history?role=dev", headers=_h(),
        )
        assert r.json()["count"] == 1

    def test_rollback_reverts_state(self, client, fake_store):
        # PATCH adds playwright
        r1 = client.patch(
            "/api/team-sessions/ses-1/template/roles/qe",
            json={"patch": {"servers": ["filesystem", "scrapling-server", "playwright"]}},
            headers=_h(),
        )
        audit_id = r1.json()["audit_ids"][0]
        # Rollback
        r2 = client.post(
            f"/api/team-sessions/ses-1/template/rollback/{audit_id}",
            json={"comment": "regret"}, headers=_h(),
        )
        assert r2.status_code == 200
        # playwright gone
        assert "playwright" not in (
            fake_store["ses-1"]["template"]["roles"]["qe"]["servers"]
        )

    def test_rollback_unknown_audit_404(self, client, fake_store):
        r = client.post(
            "/api/team-sessions/ses-1/template/rollback/9999",
            json={"comment": ""}, headers=_h(),
        )
        assert r.status_code == 404


# ── Reload ─────────────────────────────────────────────────────────────────


class TestReload:
    def test_reload_requires_confirm(self, client):
        r = client.post(
            "/api/team-sessions/ses-1/reload",
            json={"roles": ["qe"], "confirm": False},
            headers=_h(),
        )
        assert r.status_code == 400
        assert "confirm" in r.json()["detail"]

    def test_reload_requires_roles(self, client):
        r = client.post(
            "/api/team-sessions/ses-1/reload",
            json={"roles": [], "confirm": True},
            headers=_h(),
        )
        assert r.status_code == 400

    def test_reload_calls_orchestrator_with_correct_args(self, client):
        from routes import team_template as routes_mod

        fake_result = {"qe": [{"agent_name": "QE-1", "killed": True, "resumed": True}]}
        with patch.object(
            routes_mod, "reload_roles", new=AsyncMock(return_value=fake_result),
        ) as mocked:
            r = client.post(
                "/api/team-sessions/ses-1/reload",
                json={"roles": ["qe"], "confirm": True},
                headers=_h(),
            )
        assert r.status_code == 200
        assert r.json()["results"] == fake_result
        mocked.assert_called_once()
        call_kwargs = mocked.call_args.kwargs
        assert call_kwargs["session_id"] == "ses-1"
        assert call_kwargs["roles"] == ["qe"]
        assert call_kwargs["edited_by"] == "system"


# ── Reset to yaml ──────────────────────────────────────────────────────────


class TestYamlDiff:
    def test_in_sync_returns_empty_diverged(self, client, fake_store, tmp_path, monkeypatch):
        from routes import team_template as routes_mod

        yaml_path = tmp_path / "agile_team.yaml"
        # Write yaml that exactly matches the fake_store qe + dev config
        yaml_path.write_text(
            "team:\n"
            "  name: agile-team\n"
            "  roles:\n"
            "    qe:\n"
            "      role_display: QE\n"
            "      instruction: 'you are QE'\n"
            "      servers: [filesystem, scrapling-server]\n"
            "      skills: [qe-workflow]\n"
            "      model: ''\n"
            "      server_overrides: {}\n"
            "    dev:\n"
            "      role_display: Dev\n"
            "      instruction: 'you are dev'\n"
            "      servers: [filesystem, git]\n"
            "      skills: [dev-workflow]\n"
            "      model: ''\n"
            "      server_overrides: {}\n"
        )
        monkeypatch.setattr(routes_mod, "_resolve_yaml_for_session", lambda sid: yaml_path)
        r = client.get(
            "/api/team-sessions/ses-1/template/yaml-diff", headers=_h(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["in_sync"] is True
        assert body["diverged_count"] == 0
        assert all(v["status"] == "in_sync" for v in body["per_role"].values())

    def test_diverged_shows_per_field_diff(self, client, fake_store, tmp_path, monkeypatch):
        from routes import team_template as routes_mod

        # Yaml has playwright; DB doesn't
        yaml_path = tmp_path / "agile_team.yaml"
        yaml_path.write_text(
            "team:\n"
            "  name: agile-team\n"
            "  roles:\n"
            "    qe:\n"
            "      role_display: QE\n"
            "      instruction: 'you are QE'\n"
            "      servers: [filesystem, scrapling-server, playwright]\n"
            "      skills: [qe-workflow]\n"
            "      model: ''\n"
            "      server_overrides: {}\n"
        )
        monkeypatch.setattr(routes_mod, "_resolve_yaml_for_session", lambda sid: yaml_path)
        r = client.get(
            "/api/team-sessions/ses-1/template/yaml-diff", headers=_h(),
        )
        body = r.json()
        assert body["in_sync"] is False
        # qe diverged on servers (yaml has playwright that DB doesn't)
        assert body["per_role"]["qe"]["status"] == "diverged"
        assert "servers" in body["per_role"]["qe"]["fields"]
        # dev not in yaml → status added_in_db
        assert body["per_role"]["dev"]["status"] == "added_in_db"

    def test_yaml_missing_404(self, client, fake_store, tmp_path, monkeypatch):
        from routes import team_template as routes_mod

        monkeypatch.setattr(
            routes_mod, "_resolve_yaml_for_session",
            lambda sid: tmp_path / "non_existent.yaml",
        )
        r = client.get(
            "/api/team-sessions/ses-1/template/yaml-diff", headers=_h(),
        )
        assert r.status_code == 404


class TestResetRole:
    def test_reset_writes_audit_and_replaces_role(self, client, fake_store, tmp_path, monkeypatch):
        # Compose a yaml the route can load via _resolve_yaml_for_session
        from routes import team_template as routes_mod

        yaml_path = tmp_path / "agile_team.yaml"
        yaml_path.write_text(
            "team:\n"
            "  roles:\n"
            "    qe:\n"
            "      role_display: QE\n"
            "      instruction: factory qe\n"
            "      servers: [filesystem, scrapling-server, playwright]\n"
            "      skills: [qe-workflow]\n"
            "      model: ''\n"
        )
        monkeypatch.setattr(
            routes_mod, "_resolve_yaml_for_session", lambda sid: yaml_path,
        )
        r = client.post(
            "/api/team-sessions/ses-1/template/reset/qe",
            json={"comment": "back to factory"}, headers=_h(),
        )
        assert r.status_code == 200
        body = r.json()
        assert "playwright" in fake_store["ses-1"]["template"]["roles"]["qe"]["servers"]
        # at least one audit row tagged yaml-reset
        rows = client.get(
            "/api/team-sessions/ses-1/template/history?role=qe", headers=_h(),
        ).json()["rows"]
        assert any(row["source"] == "yaml-reset" for row in rows)
