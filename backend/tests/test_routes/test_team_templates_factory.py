"""Tests for routes/team_templates_factory.py + factory service.

Covers the yaml-file API surface (list / read / write) plus the safety
properties: yaml validation, path-traversal guard, .bak rotation. We point
the service at a temp directory so the real ``backend/team_templates``
yamls are never touched.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth as core_auth


_API_KEY = "unit-test-master-key-team-templates-factory"


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", _API_KEY)
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", _API_KEY)


@pytest.fixture()
def factory_dir(tmp_path, monkeypatch):
    """Redirect the factory service at a fresh temp directory and seed it
    with two valid yamls + one malformed one for read-tolerance coverage.
    """
    d = tmp_path / "team_templates"
    d.mkdir()
    (d / "agile_team.yaml").write_text(
        "name: agile-team\n"
        "description: Agile crew with PM + Dev\n"
        "orchestrator: pm\n"
        "roles:\n"
        "  pm: {role_display: PM, instruction: do orchestration}\n",
        encoding="utf-8",
    )
    (d / "research_team.yaml").write_text(
        "team:\n"
        "  name: research-team\n"
        "  description: Research crew\n"
        "  orchestrator: lead\n"
        "  roles:\n"
        "    lead: {role_display: Lead, instruction: investigate}\n",
        encoding="utf-8",
    )
    (d / "broken.yaml").write_text("name: broken\n: not valid yaml: [\n", encoding="utf-8")

    from services import team_template_factory_service as svc
    monkeypatch.setattr(svc, "_FACTORY_DIR", d.resolve())
    return d


@pytest.fixture()
def client(factory_dir):
    from routes.team_templates_factory import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _h() -> dict[str, str]:
    return {"Authorization": f"Bearer {_API_KEY}"}


# ── Auth ───────────────────────────────────────────────────────────────────


class TestAuth:
    def test_list_requires_bearer(self, client):
        assert client.get("/api/team-templates").status_code == 401

    def test_read_requires_bearer(self, client):
        assert client.get("/api/team-templates/agile_team").status_code == 401

    def test_write_requires_bearer(self, client):
        r = client.put("/api/team-templates/agile_team", json={"content": "name: x\n"})
        assert r.status_code == 401


# ── List ───────────────────────────────────────────────────────────────────


class TestList:
    def test_lists_factory_yamls(self, client):
        r = client.get("/api/team-templates", headers=_h())
        assert r.status_code == 200
        names = sorted(t["name"] for t in r.json()["templates"])
        assert names == ["agile_team", "broken", "research_team"]

    def test_picks_up_display_name_from_either_layout(self, client):
        r = client.get("/api/team-templates", headers=_h())
        by_name = {t["name"]: t for t in r.json()["templates"]}
        # Top-level "name:" layout
        assert by_name["agile_team"]["display_name"] == "agile-team"
        # "team:" nested layout
        assert by_name["research_team"]["display_name"] == "research-team"
        # Malformed → falls back to stem
        assert by_name["broken"]["display_name"] == "broken"


# ── Read ───────────────────────────────────────────────────────────────────


class TestRead:
    def test_returns_raw_and_parsed(self, client):
        r = client.get("/api/team-templates/agile_team", headers=_h())
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "agile_team"
        assert body["filename"] == "agile_team.yaml"
        assert body["parsed"]["name"] == "agile-team"
        assert "pm" in body["parsed"]["roles"]
        assert body["exists"] is True
        assert body["size"] > 0
        assert "parse_error" not in body

    def test_malformed_returns_content_with_parse_error(self, client):
        r = client.get("/api/team-templates/broken", headers=_h())
        assert r.status_code == 200
        body = r.json()
        assert body["parsed"] is None
        assert "parse_error" in body
        assert body["content"]  # raw still returned for hand-edit recovery

    def test_unknown_404(self, client):
        r = client.get("/api/team-templates/does_not_exist", headers=_h())
        assert r.status_code == 404

    def test_dotfile_name_400(self, client):
        # Encoded ../ is normalised away by starlette before matching the
        # single-segment {name} converter, so the dangerous shape never
        # reaches our handler. We assert the in-band dot-prefix guard fires
        # for the closest thing that DOES match the route.
        r = client.get("/api/team-templates/.evil", headers=_h())
        assert r.status_code == 400


# ── Write ──────────────────────────────────────────────────────────────────


class TestWrite:
    def test_overwrites_and_rotates_bak(self, client, factory_dir):
        new = "name: agile-team\ndescription: updated\nroles:\n  pm: {}\n"
        r = client.put(
            "/api/team-templates/agile_team",
            json={"content": new},
            headers=_h(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["saved"] is True
        # File rewritten
        on_disk = (factory_dir / "agile_team.yaml").read_text(encoding="utf-8")
        assert on_disk == new
        # .bak captured prior content
        backup = factory_dir / "agile_team.yaml.bak"
        assert backup.exists()
        assert "Agile crew" in backup.read_text(encoding="utf-8")

    def test_creates_new_template(self, client, factory_dir):
        r = client.put(
            "/api/team-templates/brand_new_team",
            json={"content": "name: brand-new\nroles:\n  lead: {}\n"},
            headers=_h(),
        )
        assert r.status_code == 200
        assert (factory_dir / "brand_new_team.yaml").exists()

    def test_invalid_yaml_400(self, client, factory_dir):
        r = client.put(
            "/api/team-templates/agile_team",
            json={"content": ": broken: [\n"},
            headers=_h(),
        )
        assert r.status_code == 400
        # Original content must NOT have been overwritten
        original = (factory_dir / "agile_team.yaml").read_text(encoding="utf-8")
        assert "Agile crew" in original

    def test_structurally_invalid_no_roles_400(self, client, factory_dir):
        # Parses cleanly but has no `roles` mapping — would brick spawn_team.
        # Critical: this is the path team_template_write_factory MCP tool
        # takes, so an unattended LLM must not be able to write this shape.
        r = client.put(
            "/api/team-templates/agile_team",
            json={"content": "name: agile-team\ndescription: no roles here\n"},
            headers=_h(),
        )
        assert r.status_code == 400
        assert "roles" in r.json()["detail"]["message"].lower()
        # Original content preserved
        assert "Agile crew" in (factory_dir / "agile_team.yaml").read_text(encoding="utf-8")

    def test_structurally_invalid_roles_is_list_400(self, client, factory_dir):
        # `roles:` exists but is a list, not a mapping — also brick-shaped.
        r = client.put(
            "/api/team-templates/agile_team",
            json={"content": "name: agile-team\nroles:\n  - pm\n  - dev\n"},
            headers=_h(),
        )
        assert r.status_code == 400

    def test_structurally_invalid_role_config_not_mapping_400(self, client, factory_dir):
        r = client.put(
            "/api/team-templates/agile_team",
            json={"content": "name: agile-team\nroles:\n  pm: not-a-mapping\n"},
            headers=_h(),
        )
        assert r.status_code == 400

    def test_structurally_invalid_empty_roles_400(self, client, factory_dir):
        r = client.put(
            "/api/team-templates/agile_team",
            json={"content": "name: agile-team\nroles: {}\n"},
            headers=_h(),
        )
        assert r.status_code == 400

    def test_accepts_team_nested_layout(self, client, factory_dir):
        # The other layout the spawner accepts: `team: {roles: {...}}`.
        # Must save fine (research_team.yaml uses this shape).
        new = (
            "team:\n"
            "  name: agile-team\n"
            "  roles:\n"
            "    pm:\n"
            "      role_display: PM\n"
        )
        r = client.put(
            "/api/team-templates/agile_team",
            json={"content": new},
            headers=_h(),
        )
        assert r.status_code == 200

    def test_accepts_empty_role_config(self, client, factory_dir):
        # An empty role dict is valid — the spawner falls back to defaults.
        new = "name: agile-team\nroles:\n  pm: {}\n"
        r = client.put(
            "/api/team-templates/agile_team",
            json={"content": new},
            headers=_h(),
        )
        assert r.status_code == 200

    def test_dotfile_name_blocked(self, client, factory_dir):
        r = client.put(
            "/api/team-templates/.evil",
            json={"content": "name: x\n"},
            headers=_h(),
        )
        assert r.status_code == 400
        assert not (factory_dir / ".evil.yaml").exists()


# ── Service-level: dotted name + nested name rejection ────────────────────


class TestServiceGuards:
    def test_leading_dot_rejected(self, factory_dir):
        from services import team_template_factory_service as svc
        with pytest.raises(svc.PathTraversalError):
            svc._resolve(".hidden")

    def test_slash_rejected(self, factory_dir):
        from services import team_template_factory_service as svc
        with pytest.raises(svc.PathTraversalError):
            svc._resolve("nested/path")

    def test_oversize_rejected(self, factory_dir):
        from services import team_template_factory_service as svc
        too_big = "x" * (svc.MAX_BYTES + 1)
        with pytest.raises(svc.ValidationError):
            svc.write_factory_template("agile_team", too_big)
