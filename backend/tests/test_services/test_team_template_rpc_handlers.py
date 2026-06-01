"""Tests for services/team_template_rpc_handlers.py.

The handlers wrap two services (factory yaml + DB-level running template).
We mock the store + DB the same way the route tests do and assert each
RPC method returns the documented envelope (success dict or
``{"error", "status"}``).
"""
from __future__ import annotations

import asyncio
import copy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base


# ── DB fixture (shared with route tests' style) ────────────────────────────


@pytest.fixture()
def session_factory(tmp_path, monkeypatch):
    db_file = tmp_path / "rpc_test.db"
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    import core.database as core_db
    monkeypatch.setattr(core_db, "SessionLocal", SessionFactory)
    yield SessionFactory
    engine.dispose()


# ── Fake team-sessions store ──────────────────────────────────────────────


@pytest.fixture()
def fake_store(monkeypatch):
    store_state = {
        "ses-1": {
            "session_id": "ses-1",
            "template": {
                "name": "agile-team",
                "orchestrator": "pm",
                "roles": {
                    "qe": {
                        "role_display": "QE",
                        "instruction": "you are QE",
                        "servers": ["filesystem"],
                        "skills": ["qe-workflow"],
                        "model": "",
                        "server_overrides": {},
                    },
                },
            },
        },
    }
    from services import team_template_service as svc_mod

    monkeypatch.setattr(
        svc_mod, "_get_team_session_dict",
        lambda sid: copy.deepcopy(store_state[sid]) if sid in store_state
        else (_ for _ in ()).throw(svc_mod.NotFoundError(sid)),
    )
    monkeypatch.setattr(
        svc_mod, "_put_team_session_dict",
        lambda sid, data: store_state.__setitem__(sid, copy.deepcopy(data)),
    )
    return store_state


# ── Factory yaml dir ──────────────────────────────────────────────────────


@pytest.fixture()
def factory_dir(tmp_path, monkeypatch):
    d = tmp_path / "team_templates"
    d.mkdir()
    (d / "agile_team.yaml").write_text(
        "name: agile-team\n"
        "roles:\n"
        "  qe:\n"
        "    role_display: QE\n"
        "    instruction: factory qe\n"
        "    servers: [filesystem, scrapling-server]\n"
        "    skills: [qe-workflow]\n",
        encoding="utf-8",
    )
    from services import team_template_factory_service as fsvc
    monkeypatch.setattr(fsvc, "_FACTORY_DIR", d.resolve())
    return d


# ── Tests: factory surface ────────────────────────────────────────────────


class TestFactorySurface:
    def test_list(self, factory_dir):
        from services.team_template_rpc_handlers import _factory_list

        out = _factory_list()
        names = [t["name"] for t in out["templates"]]
        assert "agile_team" in names

    def test_read_ok(self, factory_dir):
        from services.team_template_rpc_handlers import _factory_read

        out = _factory_read(name="agile_team")
        assert out["parsed"]["name"] == "agile-team"
        assert "error" not in out

    def test_read_404(self, factory_dir):
        from services.team_template_rpc_handlers import _factory_read

        out = _factory_read(name="missing")
        assert out == {"error": out["error"], "status": 404}
        assert "missing" in out["error"]

    def test_write_ok_persists_to_disk(self, factory_dir):
        from services.team_template_rpc_handlers import _factory_write

        new = "name: agile-team\nroles:\n  qe: {}\n"
        out = _factory_write(name="agile_team", content=new)
        assert out["saved"] is True
        assert (factory_dir / "agile_team.yaml").read_text(encoding="utf-8") == new

    def test_write_invalid_yaml_400(self, factory_dir):
        from services.team_template_rpc_handlers import _factory_write

        out = _factory_write(name="agile_team", content=": broken: [\n")
        assert out["status"] == 400


# ── Tests: running surface ────────────────────────────────────────────────


class TestRunningSurface:
    def test_get_returns_template(self, session_factory, fake_store):
        from services.team_template_rpc_handlers import _running_get

        out = _running_get(session_id="ses-1")
        assert out["template"]["roles"]["qe"]["servers"] == ["filesystem"]

    def test_get_unknown_session_404(self, session_factory, fake_store):
        from services.team_template_rpc_handlers import _running_get

        out = _running_get(session_id="nope")
        assert out["status"] == 404

    def test_patch_role_writes_audit_and_persists(
        self, session_factory, fake_store
    ):
        from services.team_template_rpc_handlers import _running_patch_role

        out = _running_patch_role(
            session_id="ses-1",
            role="qe",
            patch={"servers": ["filesystem", "playwright"]},
            comment="add playwright",
        )
        assert out["session_id"] == "ses-1"
        assert "audit_ids" in out and len(out["audit_ids"]) == 1
        # SSoT mutated
        assert "playwright" in fake_store["ses-1"]["template"]["roles"]["qe"]["servers"]

    def test_patch_role_invalid_field_400(self, session_factory, fake_store):
        from services.team_template_rpc_handlers import _running_patch_role

        out = _running_patch_role(
            session_id="ses-1",
            role="qe",
            patch={"not_a_field": True},
        )
        assert out["status"] == 400

    def test_patch_role_unknown_role_404(self, session_factory, fake_store):
        from services.team_template_rpc_handlers import _running_patch_role

        out = _running_patch_role(
            session_id="ses-1",
            role="unknown",
            patch={"servers": ["x"]},
        )
        assert out["status"] == 404

    def test_history_then_rollback(self, session_factory, fake_store):
        from services.team_template_rpc_handlers import (
            _running_history,
            _running_patch_role,
            _running_rollback,
        )

        patch_out = _running_patch_role(
            session_id="ses-1",
            role="qe",
            patch={"instruction": "edited via mcp"},
        )
        audit_id = patch_out["audit_ids"][0]

        hist = _running_history(session_id="ses-1")
        assert hist["count"] >= 1

        rb = _running_rollback(session_id="ses-1", audit_id=audit_id)
        assert "audit_ids" in rb
        # Rolled back to original value
        assert (
            fake_store["ses-1"]["template"]["roles"]["qe"]["instruction"]
            == "you are QE"
        )

    def test_yaml_diff_detects_drift(
        self, session_factory, fake_store, factory_dir
    ):
        from services.team_template_rpc_handlers import _running_yaml_diff

        out = _running_yaml_diff(session_id="ses-1")
        # Factory yaml has servers=[filesystem, scrapling-server], DB has [filesystem]
        assert out["in_sync"] is False
        assert out["per_role"]["qe"]["status"] == "diverged"
        assert "servers" in out["per_role"]["qe"]["fields"]

    def test_reset_role_to_yaml_factory(
        self, session_factory, fake_store, factory_dir
    ):
        from services.team_template_rpc_handlers import _running_reset_role

        out = _running_reset_role(session_id="ses-1", role="qe")
        assert "audit_ids" in out
        # DB role now matches the factory yaml
        assert sorted(
            fake_store["ses-1"]["template"]["roles"]["qe"]["servers"]
        ) == sorted(["filesystem", "scrapling-server"])

    def test_reload_empty_roles_400(self, session_factory, fake_store):
        from services.team_template_rpc_handlers import _running_reload

        out = asyncio.run(_running_reload(session_id="ses-1", roles=[]))
        assert out["status"] == 400


# ── Registration ──────────────────────────────────────────────────────────


class TestRegistration:
    def test_register_attaches_every_method(self):
        from services import team_template_rpc_handlers as rpc

        registered: dict[str, object] = {}

        class FakeServer:
            def register(self, name, handler, **kw):
                registered[name] = handler

        rpc.register(FakeServer())
        for method in rpc._METHODS:
            assert method in registered, f"missing RPC method {method}"
