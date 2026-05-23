"""Tests for ``services.skill_rpc_handlers``.

The handlers are what the RPC bridge invokes in the main backend
process. They wrap ``skill_service`` and write the side-effect
notification rows. Both are real here — no service mock — because the
whole point of the RPC layer is that mutation runs in this process,
including ``rebuild_agent_instruction``. Tests use tmp dirs + an isolated
SQLite engine so nothing leaks into the real backend state.
"""
from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.database import Base, NotificationModel
from services import skill_rpc_handlers as h
from services import skill_service as svc


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    from core import database as db_mod
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    class _Cm:
        def __enter__(self): self.s = SessionLocal(); return self.s
        def __exit__(self, *a): self.s.close()

    monkeypatch.setattr(db_mod, "get_db_session", lambda: _Cm())
    monkeypatch.setattr(h, "get_db_session", lambda: _Cm())
    return SessionLocal


@pytest.fixture()
def fake_skills_dir(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    builtin = skills_dir / "_builtin.yaml"
    builtin.write_text("builtin:\n  - audio-reading\n", encoding="utf-8")
    # Bind a fresh DB for the agent_definitions store so used-by lookups
    # during skill_delete don't see stray state from another test.
    db_path = str(tmp_path / "test_skill_rpc.db")
    monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)
    monkeypatch.setattr(svc, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(svc, "BUILTIN_MANIFEST", builtin)
    monkeypatch.setattr(svc, "_builtin_cache", None)
    monkeypatch.setattr(svc, "_builtin_mtime_ns", 0)
    monkeypatch.setattr(svc, "_runtime_handles", lambda: (None, None, None))
    return {"skills": skills_dir}


def _write_skill(skills_dir, name):
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        dedent(f"""\
        ---
        name: {name}
        description: ok
        ---
        body
        """),
        encoding="utf-8",
    )


def _count_notifications(SessionLocal, *, kind: str | None = None) -> int:
    s = SessionLocal()
    try:
        rows = s.query(NotificationModel).all()
        if kind is None:
            return len(rows)
        import json
        return sum(1 for r in rows if (json.loads(r.metadata_json or "{}").get("kind") == kind))
    finally:
        s.close()


# ============================================================
# Read handlers (sync)
# ============================================================


class TestReadHandlers:
    def test_skill_list(self, fake_skills_dir):
        _write_skill(fake_skills_dir["skills"], "demo")
        result = h.skill_list()
        assert {s["name"] for s in result["skills"]} == {"demo"}

    def test_skill_get_returns_content(self, fake_skills_dir):
        _write_skill(fake_skills_dir["skills"], "demo")
        result = h.skill_get(name="demo")
        assert result["name"] == "demo"
        assert "body" in result["content"]

    def test_skill_get_unknown_returns_error(self, fake_skills_dir):
        result = h.skill_get(name="missing")
        assert result["status"] == 404


# ============================================================
# Mutating handlers
# ============================================================


class TestMutatingHandlers:
    def test_create_writes_disk_and_notification(self, fake_skills_dir, isolated_db):
        result = h.skill_create(name="brand-new", description="d", body="hello")
        assert result == {"created": True, "name": "brand-new", "is_builtin": False}
        assert (fake_skills_dir["skills"] / "brand-new" / "SKILL.md").exists()
        assert _count_notifications(isolated_db, kind="skill_create") == 1

    def test_create_validation_error_no_notification(self, fake_skills_dir, isolated_db):
        result = h.skill_create(name="Bad Name", description="d", body="b")
        assert result["status"] == 400
        assert _count_notifications(isolated_db, kind="skill_create") == 0

    @pytest.mark.asyncio
    async def test_update_writes_notification(self, fake_skills_dir, isolated_db):
        _write_skill(fake_skills_dir["skills"], "demo")
        new_content = "---\nname: demo\ndescription: new\n---\nbody\n"
        result = await h.skill_update(name="demo", content=new_content)
        assert result == {"updated": True, "name": "demo"}
        assert _count_notifications(isolated_db, kind="skill_update") == 1

    @pytest.mark.asyncio
    async def test_delete_user_skill_writes_notification(self, fake_skills_dir, isolated_db):
        _write_skill(fake_skills_dir["skills"], "user-skill")
        result = await h.skill_delete(name="user-skill")
        assert result["deleted"] is True
        assert _count_notifications(isolated_db, kind="skill_delete") == 1

    @pytest.mark.asyncio
    async def test_delete_builtin_blocked_no_notification(self, fake_skills_dir, isolated_db):
        _write_skill(fake_skills_dir["skills"], "audio-reading")
        result = await h.skill_delete(name="audio-reading")
        assert result["status"] == 403
        assert _count_notifications(isolated_db, kind="skill_delete") == 0


# ============================================================
# Registration smoke
# ============================================================


def test_register_attaches_seven_methods():
    class _StubServer:
        def __init__(self):
            self.registered = {}
        def register(self, method, handler):
            self.registered[method] = handler

    s = _StubServer()
    h.register(s)
    assert set(s.registered) == {
        "skill.list", "skill.get", "skill.create",
        "skill.update", "skill.delete",
        "skill.attach", "skill.detach",
    }
