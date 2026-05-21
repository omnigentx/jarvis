"""Unit tests for services.team_template_service.

Pin the invariants users rely on every time they edit a template:
  - diff is order-insensitive for servers/skills
  - validate rejects unknown fields + phantom paths
  - apply writes audit rows in the SAME transaction as the team_sessions update
  - rollback writes a NEW history row, never deletes
  - reset-from-yaml only touches the named role

Uses an in-memory SQLite for the audit table; the team_sessions read/write
path is mocked because that lives in the fast_agent submodule and we don't
want to drag in the whole spawn stack for service-level unit tests.
"""
from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, TeamTemplateHistory
from services import team_template_service as svc


# ── shared fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def db_session():
    """Fresh in-memory SQLite + create_all so TeamTemplateHistory exists."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def fake_team_data():
    """A minimal team_sessions row mirroring the real shape."""
    return {
        "session_id": "ses-1",
        "team_name": "test-team",
        "workspace": "/tmp/ws",
        "project_brief": "x",
        "parent_session_id": "",
        "conversation_id": "",
        "agents": {},
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
                    "server_overrides": {
                        "filesystem": {
                            "args": [
                                "-y",
                                "@modelcontextprotocol/server-filesystem",
                                "{workspace_dir}",
                            ],
                        },
                    },
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
    }


@pytest.fixture
def patched_store(fake_team_data, monkeypatch):
    """Patch get_team_session + _get_store to use an in-memory dict."""
    store_state = {"ses-1": copy.deepcopy(fake_team_data)}

    class FakeStore:
        def upsert(self, sid, data):
            store_state[sid] = copy.deepcopy(data)

        def get(self, sid):
            return copy.deepcopy(store_state.get(sid))

    class FakeSession:
        def __init__(self, data):
            self._data = data

        def to_dict(self):
            return copy.deepcopy(self._data)

    def fake_get_team_session(sid):
        data = store_state.get(sid)
        if data is None:
            return None
        return FakeSession(data)

    def fake_get_store():
        return FakeStore()

    with patch.object(
        svc, "_get_team_session_dict",
        side_effect=lambda sid: (
            store_state[sid] if sid in store_state
            else (_ for _ in ()).throw(svc.NotFoundError(f"team session '{sid}' not found"))
        ),
    ), patch.object(
        svc, "_put_team_session_dict",
        side_effect=lambda sid, data: store_state.__setitem__(sid, copy.deepcopy(data)),
    ):
        yield store_state


# ── compute_role_diff ──────────────────────────────────────────────────────


class TestComputeRoleDiff:
    def test_identical_returns_empty(self):
        b = {"servers": ["a", "b"], "instruction": "x"}
        assert svc.compute_role_diff(b, dict(b)) == {}

    def test_servers_reorder_is_not_a_change(self):
        before = {"servers": ["a", "b", "c"]}
        after = {"servers": ["c", "a", "b"]}
        assert svc.compute_role_diff(before, after) == {}

    def test_skills_reorder_is_not_a_change(self):
        before = {"skills": ["one", "two"]}
        after = {"skills": ["two", "one"]}
        assert svc.compute_role_diff(before, after) == {}

    def test_servers_added(self):
        before = {"servers": ["a"]}
        after = {"servers": ["a", "b"]}
        diff = svc.compute_role_diff(before, after)
        assert "servers" in diff
        assert sorted(diff["servers"]["before"]) == ["a"]
        assert sorted(diff["servers"]["after"]) == ["a", "b"]

    def test_instruction_changed(self):
        diff = svc.compute_role_diff({"instruction": "x"}, {"instruction": "y"})
        assert diff == {"instruction": {"before": "x", "after": "y"}}

    def test_nested_overrides_deep_equal(self):
        b = {"server_overrides": {"fs": {"args": ["a", "b"]}}}
        a = {"server_overrides": {"fs": {"args": ["a", "b"]}}}
        assert svc.compute_role_diff(b, a) == {}

    def test_nested_overrides_change(self):
        b = {"server_overrides": {"fs": {"args": ["a"]}}}
        a = {"server_overrides": {"fs": {"args": ["a", "b"]}}}
        diff = svc.compute_role_diff(b, a)
        assert "server_overrides" in diff


# ── validate_patch ─────────────────────────────────────────────────────────


class TestValidatePatch:
    def test_accepts_known_fields(self):
        svc.validate_patch("qe", {"servers": ["a"], "instruction": "x", "skills": ["s"]})
        svc.validate_patch("qe", {"server_overrides": {"fs": {"args": ["{workspace_dir}"]}}})

    def test_rejects_unknown_field(self):
        with pytest.raises(svc.ValidationError, match="unknown fields"):
            svc.validate_patch("qe", {"cwd": "/etc"})

    def test_servers_must_be_list_of_strings(self):
        with pytest.raises(svc.ValidationError, match="'servers' must be"):
            svc.validate_patch("qe", {"servers": "filesystem"})
        with pytest.raises(svc.ValidationError, match="'servers' must be"):
            svc.validate_patch("qe", {"servers": ["", "filesystem"]})

    def test_skills_must_be_list_of_strings(self):
        with pytest.raises(svc.ValidationError, match="'skills' must be"):
            svc.validate_patch("qe", {"skills": [123]})

    def test_phantom_path_rejected(self, tmp_path):
        patch_obj = {
            "server_overrides": {
                "fs": {
                    "args": [
                        "-y",
                        "@modelcontextprotocol/server-filesystem",
                        str(tmp_path / "does_not_exist_for_sure"),
                    ],
                },
            },
        }
        with pytest.raises(svc.ValidationError, match="does not exist"):
            svc.validate_patch("qe", patch_obj, project_dir=tmp_path)

    def test_template_placeholder_accepted(self, tmp_path):
        # {workspace_dir} is substituted at spawn — must NOT be path-checked
        patch_obj = {
            "server_overrides": {
                "fs": {"args": ["-y", "@x/y", "{workspace_dir}", "{project_dir}/skills"]},
            },
        }
        svc.validate_patch("qe", patch_obj, project_dir=tmp_path)  # no raise

    def test_existing_path_accepted(self, tmp_path):
        target = tmp_path / "real_dir"
        target.mkdir()
        patch_obj = {
            "server_overrides": {"fs": {"args": [str(target)]}},
        }
        svc.validate_patch("qe", patch_obj, project_dir=tmp_path)  # no raise


# ── apply_role_patch ───────────────────────────────────────────────────────


class TestApplyRolePatch:
    def test_one_field_writes_one_audit_row(self, db_session, patched_store):
        result = svc.apply_role_patch(
            db_session, "ses-1", "qe",
            {"servers": ["filesystem", "scrapling-server", "playwright"]},
            edited_by="alice", source="api", comment="add playwright",
        )
        assert len(result["audit_ids"]) == 1
        assert "servers" in result["diff"]
        # State persisted
        assert "playwright" in patched_store["ses-1"]["template"]["roles"]["qe"]["servers"]
        # Audit row exists
        rows = db_session.query(TeamTemplateHistory).all()
        assert len(rows) == 1
        assert rows[0].role == "qe"
        assert rows[0].field == "servers"
        assert rows[0].edited_by == "alice"
        assert rows[0].source == "api"

    def test_multiple_fields_write_multiple_rows_one_commit(
        self, db_session, patched_store,
    ):
        result = svc.apply_role_patch(
            db_session, "ses-1", "qe",
            {"servers": ["a"], "instruction": "new", "skills": ["s2"]},
            edited_by="bob",
        )
        assert len(result["audit_ids"]) == 3
        fields = {r.field for r in db_session.query(TeamTemplateHistory).all()}
        assert fields == {"servers", "instruction", "skills"}

    def test_noop_raises_conflict(self, db_session, patched_store):
        # Same servers in different order = NOOP per order-insensitive diff
        with pytest.raises(svc.ConflictError):
            svc.apply_role_patch(
                db_session, "ses-1", "qe",
                {"servers": ["scrapling-server", "filesystem"]},
            )
        # No rows written
        assert db_session.query(TeamTemplateHistory).count() == 0

    def test_unknown_role_raises(self, db_session, patched_store):
        with pytest.raises(svc.NotFoundError):
            svc.apply_role_patch(db_session, "ses-1", "unknown", {"servers": ["a"]})

    def test_invalid_patch_rejected_before_db_write(
        self, db_session, patched_store,
    ):
        with pytest.raises(svc.ValidationError):
            svc.apply_role_patch(db_session, "ses-1", "qe", {"unknown_field": 1})
        assert db_session.query(TeamTemplateHistory).count() == 0


# ── get_history ────────────────────────────────────────────────────────────


class TestGetHistory:
    def test_empty_history(self, db_session, patched_store):
        assert svc.get_history(db_session, "ses-1") == []

    def test_newest_first(self, db_session, patched_store):
        svc.apply_role_patch(db_session, "ses-1", "qe", {"servers": ["a"]})
        svc.apply_role_patch(db_session, "ses-1", "qe", {"servers": ["a", "b"]})
        rows = svc.get_history(db_session, "ses-1")
        assert len(rows) == 2
        assert rows[0]["edited_at"] >= rows[1]["edited_at"]

    def test_filter_by_role(self, db_session, patched_store):
        svc.apply_role_patch(db_session, "ses-1", "qe", {"servers": ["x"]})
        svc.apply_role_patch(db_session, "ses-1", "dev", {"servers": ["y"]})
        rows = svc.get_history(db_session, "ses-1", role="dev")
        assert len(rows) == 1
        assert rows[0]["role"] == "dev"

    def test_before_after_decoded(self, db_session, patched_store):
        svc.apply_role_patch(
            db_session, "ses-1", "qe",
            {"servers": ["filesystem", "scrapling-server", "playwright"]},
        )
        rows = svc.get_history(db_session, "ses-1")
        # The 'before' must equal what was originally in fake_team_data — list of 2
        assert sorted(rows[0]["before"]) == ["filesystem", "scrapling-server"]
        assert "playwright" in rows[0]["after"]


# ── rollback_to ────────────────────────────────────────────────────────────


class TestRollback:
    def test_rollback_writes_new_row_does_not_delete(self, db_session, patched_store):
        # Add playwright
        r1 = svc.apply_role_patch(
            db_session, "ses-1", "qe",
            {"servers": ["filesystem", "scrapling-server", "playwright"]},
        )
        audit_id = r1["audit_ids"][0]
        # Rollback
        r2 = svc.rollback_to(
            db_session, "ses-1", audit_id, edited_by="charlie", comment="oops",
        )
        # Two history rows total
        all_rows = db_session.query(TeamTemplateHistory).all()
        assert len(all_rows) == 2
        # New row tagged 'rollback' with reference comment
        latest = max(all_rows, key=lambda r: r.id)
        assert latest.source == "rollback"
        assert f"audit_id={audit_id}" in latest.comment
        # State reverted
        assert "playwright" not in patched_store["ses-1"]["template"]["roles"]["qe"]["servers"]

    def test_rollback_to_unknown_audit_raises(self, db_session, patched_store):
        with pytest.raises(svc.NotFoundError):
            svc.rollback_to(db_session, "ses-1", 9999)

    def test_rollback_noop_raises_conflict(self, db_session, patched_store):
        r1 = svc.apply_role_patch(db_session, "ses-1", "qe", {"servers": ["a"]})
        svc.rollback_to(db_session, "ses-1", r1["audit_ids"][0])
        # Second rollback to same audit = NOOP (state already reverted)
        with pytest.raises(svc.ConflictError):
            svc.rollback_to(db_session, "ses-1", r1["audit_ids"][0])


# ── reset_role_to_yaml ─────────────────────────────────────────────────────


class TestResetToYaml:
    def test_reset_one_role_does_not_touch_others(
        self, db_session, patched_store, tmp_path,
    ):
        # Edit dev → diverge from yaml
        svc.apply_role_patch(db_session, "ses-1", "dev", {"instruction": "edited"})
        # Compose a yaml with only 'qe' definition
        yaml_path = tmp_path / "agile_team.yaml"
        yaml_path.write_text(
            "team:\n"
            "  roles:\n"
            "    qe:\n"
            "      role_display: QE\n"
            "      instruction: factory qe\n"
            "      servers: [filesystem, scrapling-server]\n"
            "      skills: [qe-workflow]\n"
            "      model: ''\n"
        )
        result = svc.reset_role_to_yaml(db_session, "ses-1", "qe", yaml_path)
        # qe reset
        assert patched_store["ses-1"]["template"]["roles"]["qe"]["instruction"] == "factory qe"
        # dev untouched
        assert patched_store["ses-1"]["template"]["roles"]["dev"]["instruction"] == "edited"
        # Audit row tagged yaml-reset
        rows = svc.get_history(db_session, "ses-1", role="qe")
        assert any(r["source"] == "yaml-reset" for r in rows)

    def test_reset_role_missing_in_yaml_raises(self, db_session, patched_store, tmp_path):
        yaml_path = tmp_path / "x.yaml"
        yaml_path.write_text("team:\n  roles: {}\n")
        with pytest.raises(svc.NotFoundError):
            svc.reset_role_to_yaml(db_session, "ses-1", "qe", yaml_path)


# ── static audit: no caller of run_isolated_agent_background drops overrides ─


def test_audit_inject_resume_still_forwards_server_overrides():
    """Phase 1 must NOT regress the inject_resume fix from earlier today.

    We already have a static audit test in test_inject_resume.py; this one
    is a tighter pin: parse the call site directly, assert the kwarg is
    forwarded to ``run_isolated_agent_background``. Belt-and-suspenders;
    the two together catch refactors that rename the kwarg as well as
    ones that drop it.

    After the live-template SSoT refactor the value is resolved via the
    local ``_resolve("server_overrides", None)`` helper rather than read
    directly from ``original_config`` — but the call-site contract that
    matters here is "the kwarg is present and is bound to a local of the
    same name". Pin BOTH the resolver call AND the kwarg forward, so
    either kind of regression fails loudly.
    """
    p = Path(__file__).resolve().parents[2] / "services" / "inject_resume.py"
    src = p.read_text()
    assert "_resolve(\"server_overrides\"" in src, (
        "inject_resume.py:resume_with_inject must resolve server_overrides "
        "through the SSoT-aware _resolve() helper (prefers live "
        "team_sessions.template, falls back to original_config snapshot)."
    )
    assert "server_overrides=server_overrides" in src, (
        "inject_resume.py:resume_with_inject must forward "
        "server_overrides=server_overrides to run_isolated_agent_background — "
        "without it, the dashboard inject path silently drops per-role MCP "
        "arg customisations (incident 2026-05-17)."
    )
