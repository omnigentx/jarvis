"""Tests for routes.skills — skill CRUD with built-in protection.

Covers all edge cases A-H from the design plan: name validation, CRUD happy
paths, frontmatter validation, optimistic locking, built-in delete protection,
agent-card cleanup on delete, path traversal defence.
"""
from __future__ import annotations

import time
from pathlib import Path
from textwrap import dedent

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth as core_auth
from services import skill_service as svc
from routes import skills as skill_routes


_KEY = "skill-tests-master-key-xxxxx"
AUTH = {"Authorization": f"Bearer {_KEY}"}


@pytest.fixture(autouse=True)
def _set_master_key(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", _KEY)
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", _KEY)


def _write_skill(skills_dir: Path, name: str, *, description: str = "Demo skill", body: str = "Body text") -> Path:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(
        dedent(f"""\
        ---
        name: {name}
        description: {description}
        ---

        {body}
        """),
        encoding="utf-8",
    )
    return md


def _write_agent_card(cards_dir: Path, agent: str, skill_names: list[str]) -> Path:
    cards_dir.mkdir(parents=True, exist_ok=True)
    card = cards_dir / f"{agent}.md"
    skills_yaml = "\n".join(f"  - .fast-agent/skills/{s}" for s in skill_names)
    # Built without dedent to avoid common-indent surprises when the
    # interpolated `skills_yaml` lines have shallower indent than the
    # surrounding template.
    card.write_text(
        f"---\nname: {agent}\ninstruction: Test agent.\nskills:\n{skills_yaml}\n---\n",
        encoding="utf-8",
    )
    return card


@pytest.fixture()
def fake_dirs(tmp_path, monkeypatch):
    """Redirect SKILLS_DIR / AGENT_CARDS_DIR / BUILTIN_MANIFEST / AGENT_CODE_FILE
    to throwaway temp paths so tests don't touch real repo files.
    """
    skills_dir = tmp_path / "skills"
    cards_dir = tmp_path / "agent_cards"
    skills_dir.mkdir()
    cards_dir.mkdir()
    builtin = skills_dir / "_builtin.yaml"
    builtin.write_text("builtin:\n  - audio-reading\n  - finance\n", encoding="utf-8")
    code_file = tmp_path / "agent.py"
    code_file.write_text(
        # A small fake agent.py: PersonalAgent uses audio-reading.
        'fast = FastAgent()\n'
        '@fast.agent(\n'
        '    name="PersonalAgent",\n'
        '    instruction="...",\n'
        '    skills=get_skills("audio-reading"),\n'
        ')\n'
        'async def personal(prompt): pass\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(svc, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(svc, "AGENT_CARDS_DIR", cards_dir)
    monkeypatch.setattr(svc, "BUILTIN_MANIFEST", builtin)
    monkeypatch.setattr(svc, "AGENT_CODE_FILE", code_file)
    # Reset the internal builtin cache so each test re-reads the manifest.
    monkeypatch.setattr(svc, "_builtin_cache", None)
    monkeypatch.setattr(svc, "_builtin_mtime_ns", 0)
    # Default: no runtime. Without this, _default_runtime_handles imports the
    # real `agent` module — pulling Jarvis et al into the test's view of
    # fast.agents and silently mixing real-runtime state into assertions.
    # Tests that need a runtime opt in via _wire_attach_runtime / _wire_real_runtime.
    monkeypatch.setattr(svc, "_runtime_handles", lambda: (None, None, None))
    return {"skills": skills_dir, "cards": cards_dir, "builtin": builtin, "code": code_file}


@pytest.fixture()
def client(fake_dirs):
    app = FastAPI()
    app.include_router(skill_routes.router)
    return TestClient(app)


# ============================================================
# A. Name validation
# ============================================================


class TestNameValidation:
    @pytest.mark.parametrize("bad_name", [
        "",                # empty
        "..",              # traversal
        "../foo",          # traversal
        "foo/bar",         # slash
        "foo\\bar",        # backslash
        "Foo",             # uppercase
        "foo bar",         # space
        "foo$",            # special char
        "-foo",            # leading hyphen
        "foo-",            # trailing hyphen
        "a" * 65,          # too long
        "con",             # reserved
        "_builtin",        # reserved
        "café",            # non-ASCII
    ])
    def test_invalid_names_rejected_at_service(self, bad_name):
        # Service-level check — covers HTTP route and CLI/internal usage alike.
        with pytest.raises(svc.SkillValidationError) as excinfo:
            svc.validate_skill_name(bad_name)
        assert excinfo.value.status_code == 400

    @pytest.mark.parametrize("bad_name", [
        "Foo", "foo bar", "foo$", "-foo", "foo-", "a" * 65, "con", "_builtin",
    ])
    def test_invalid_names_rejected_on_route(self, client, bad_name):
        # Routes that go through `/{name}` paths must return 400 for invalid
        # names that *do* reach the handler. ``..``, empty, and slashes never
        # reach the handler — Starlette's router normalises them — so they're
        # covered service-side above.
        resp = client.get(f"/api/skills/{bad_name}", headers=AUTH)
        assert resp.status_code == 400, f"name={bad_name!r} got {resp.status_code}"

    @pytest.mark.parametrize("good_name", [
        "a",
        "ab",
        "audio-reading",
        "my-skill",
        "skill1",
        "1skill",
        "abc-def-ghi",
        "a" * 64,
    ])
    def test_valid_names_pass(self, good_name):
        svc.validate_skill_name(good_name)  # must not raise


# ============================================================
# B. Auth
# ============================================================


class TestAuth:
    def test_list_requires_auth(self, client):
        assert client.get("/api/skills").status_code == 401

    def test_get_requires_auth(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "demo")
        assert client.get("/api/skills/demo").status_code == 401

    def test_create_requires_auth(self, client):
        body = {"name": "x", "content": "---\nname: x\ndescription: y\n---\n"}
        assert client.post("/api/skills", json=body).status_code == 401

    def test_update_requires_auth(self, client):
        assert client.put("/api/skills/x", json={"content": ""}).status_code == 401

    def test_delete_requires_auth(self, client):
        assert client.delete("/api/skills/x").status_code == 401


# ============================================================
# C. List
# ============================================================


class TestList:
    def test_list_returns_skills_with_builtin_flag(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "audio-reading")  # builtin
        _write_skill(fake_dirs["skills"], "my-custom")      # user
        resp = client.get("/api/skills", headers=AUTH)
        assert resp.status_code == 200
        skills = {s["name"]: s for s in resp.json()["skills"]}
        assert skills["audio-reading"]["is_builtin"] is True
        assert skills["my-custom"]["is_builtin"] is False
        # mtime_ns is serialised as a string (JS Number.MAX_SAFE_INTEGER guard).
        assert isinstance(skills["my-custom"]["mtime_ns"], str)
        assert int(skills["my-custom"]["mtime_ns"]) > 0

    def test_list_skips_hidden_and_invalid_dirs(self, client, fake_dirs):
        skills = fake_dirs["skills"]
        # _builtin.yaml is a file (hidden via _ prefix) — skipped.
        # Add some weird dirs:
        (skills / ".hidden").mkdir()
        (skills / "Invalid_Name").mkdir()
        (skills / "good-skill").mkdir()
        (skills / "good-skill" / "SKILL.md").write_text(
            "---\nname: good-skill\ndescription: ok\n---\nbody\n", encoding="utf-8"
        )
        resp = client.get("/api/skills", headers=AUTH)
        names = [s["name"] for s in resp.json()["skills"]]
        assert names == ["good-skill"]

    def test_list_skips_dirs_without_skill_md(self, client, fake_dirs):
        (fake_dirs["skills"] / "empty-dir").mkdir()
        resp = client.get("/api/skills", headers=AUTH)
        names = [s["name"] for s in resp.json()["skills"]]
        assert "empty-dir" not in names

    def test_list_returns_parse_error_for_bad_frontmatter(self, client, fake_dirs):
        d = fake_dirs["skills"] / "broken"
        d.mkdir()
        (d / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")
        resp = client.get("/api/skills", headers=AUTH)
        broken = next(s for s in resp.json()["skills"] if s["name"] == "broken")
        assert broken["parse_error"] is not None
        assert broken["description"] is None

    def test_list_used_by_includes_code_and_card_agents(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "audio-reading")
        _write_agent_card(fake_dirs["cards"], "ResearchAgent", ["audio-reading"])
        resp = client.get("/api/skills", headers=AUTH)
        skill = next(s for s in resp.json()["skills"] if s["name"] == "audio-reading")
        # PersonalAgent comes from the fake agent.py; ResearchAgent from the card.
        assert set(skill["used_by"]) == {"PersonalAgent", "ResearchAgent"}


# ============================================================
# D. Read single
# ============================================================


class TestRead:
    def test_read_existing(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "demo", description="Demo desc")
        resp = client.get("/api/skills/demo", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "demo"
        assert body["description"] == "Demo desc"
        assert "Body text" in body["content"]
        assert body["is_builtin"] is False
        assert isinstance(body["mtime_ns"], str)
        assert int(body["mtime_ns"]) > 0

    def test_read_missing_404(self, client, fake_dirs):
        assert client.get("/api/skills/nope", headers=AUTH).status_code == 404


# ============================================================
# E. Create
# ============================================================


def _valid_content(name: str, desc: str = "ok") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\nBody.\n"


class TestCreate:
    def test_create_happy_path(self, client, fake_dirs):
        body = {"name": "new-skill", "content": _valid_content("new-skill")}
        resp = client.post("/api/skills", json=body, headers=AUTH)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "new-skill"
        assert data["is_builtin"] is False
        assert (fake_dirs["skills"] / "new-skill" / "SKILL.md").exists()

    def test_create_duplicate_409(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "exists")
        body = {"name": "exists", "content": _valid_content("exists")}
        resp = client.post("/api/skills", json=body, headers=AUTH)
        assert resp.status_code == 409

    def test_create_case_insensitive_collision_409(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "myskill")
        body = {"name": "myskill", "content": _valid_content("myskill")}
        # Same lowercase already exists → 409.
        resp = client.post("/api/skills", json=body, headers=AUTH)
        assert resp.status_code == 409

    def test_create_invalid_name_400(self, client):
        body = {"name": "Bad Name", "content": _valid_content("Bad Name")}
        resp = client.post("/api/skills", json=body, headers=AUTH)
        assert resp.status_code == 400

    def test_create_missing_frontmatter_400(self, client):
        body = {"name": "x", "content": "no frontmatter\n"}
        resp = client.post("/api/skills", json=body, headers=AUTH)
        assert resp.status_code == 400

    def test_create_frontmatter_name_mismatch_400(self, client):
        body = {"name": "x", "content": "---\nname: y\ndescription: d\n---\n"}
        resp = client.post("/api/skills", json=body, headers=AUTH)
        assert resp.status_code == 400

    def test_create_missing_description_400(self, client):
        body = {"name": "x", "content": "---\nname: x\n---\n"}
        resp = client.post("/api/skills", json=body, headers=AUTH)
        assert resp.status_code == 400

    def test_create_oversize_422(self, client):
        body = {"name": "x", "content": "a" * (svc.MAX_BYTES + 1)}
        resp = client.post("/api/skills", json=body, headers=AUTH)
        # Pydantic max_length kicks in first.
        assert resp.status_code == 422

    def test_create_invalid_yaml_in_frontmatter_400(self, client):
        body = {"name": "x", "content": "---\nname: x\ndescription: [unterminated\n---\n"}
        resp = client.post("/api/skills", json=body, headers=AUTH)
        assert resp.status_code == 400


# ============================================================
# F. Update
# ============================================================


class TestUpdate:
    def test_update_happy_path(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "demo", description="Old")
        # Read to grab mtime — comes back as a string; pass it through verbatim.
        get_resp = client.get("/api/skills/demo", headers=AUTH).json()
        assert isinstance(get_resp["mtime_ns"], str)
        new_content = _valid_content("demo", desc="New")
        resp = client.put(
            "/api/skills/demo",
            json={"content": new_content, "expected_mtime_ns": get_resp["mtime_ns"]},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == "New"

    def test_update_accepts_expected_mtime_as_int(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "demo")
        # Backwards-compat with curl-style integer payloads.
        from services.skill_service import _skill_md_path  # type: ignore
        mtime = _skill_md_path("demo").stat().st_mtime_ns
        resp = client.put(
            "/api/skills/demo",
            json={"content": _valid_content("demo"), "expected_mtime_ns": mtime},
            headers=AUTH,
        )
        assert resp.status_code == 200

    def test_update_rejects_non_numeric_mtime_400(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "demo")
        resp = client.put(
            "/api/skills/demo",
            json={"content": _valid_content("demo"), "expected_mtime_ns": "not-a-number"},
            headers=AUTH,
        )
        assert resp.status_code == 400

    def test_update_missing_404(self, client):
        body = {"content": _valid_content("nope"), "expected_mtime_ns": None}
        resp = client.put("/api/skills/nope", json=body, headers=AUTH)
        assert resp.status_code == 404

    def test_update_mtime_conflict_409(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "demo")
        body = {"content": _valid_content("demo"), "expected_mtime_ns": 1}
        resp = client.put("/api/skills/demo", json=body, headers=AUTH)
        assert resp.status_code == 409

    def test_update_renaming_blocked_400(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "demo")
        body = {"content": _valid_content("renamed"), "expected_mtime_ns": None}
        resp = client.put("/api/skills/demo", json=body, headers=AUTH)
        assert resp.status_code == 400

    def test_update_missing_description_400(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "demo")
        body = {"content": "---\nname: demo\n---\n", "expected_mtime_ns": None}
        resp = client.put("/api/skills/demo", json=body, headers=AUTH)
        assert resp.status_code == 400

    def test_update_builtin_allowed(self, client, fake_dirs):
        # audio-reading is in the builtin manifest — editing must still work.
        _write_skill(fake_dirs["skills"], "audio-reading")
        body = {"content": _valid_content("audio-reading", desc="updated"), "expected_mtime_ns": None}
        resp = client.put("/api/skills/audio-reading", json=body, headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["is_builtin"] is True

    def test_update_creates_backup(self, client, fake_dirs):
        md = _write_skill(fake_dirs["skills"], "demo", description="Old")
        body = {"content": _valid_content("demo", desc="New"), "expected_mtime_ns": None}
        client.put("/api/skills/demo", json=body, headers=AUTH)
        backup = md.with_suffix(md.suffix + ".bak")
        assert backup.exists()
        assert "Old" in backup.read_text(encoding="utf-8")


# ============================================================
# G. Delete
# ============================================================


class TestDelete:
    def test_delete_user_skill_happy(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "user-skill")
        resp = client.delete("/api/skills/user-skill", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert not (fake_dirs["skills"] / "user-skill").exists()

    def test_delete_builtin_403(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "audio-reading")
        resp = client.delete("/api/skills/audio-reading", headers=AUTH)
        assert resp.status_code == 403
        # Files must still be on disk.
        assert (fake_dirs["skills"] / "audio-reading").exists()

    def test_delete_missing_404(self, client):
        assert client.delete("/api/skills/nope", headers=AUTH).status_code == 404

    def test_delete_invalid_name_400(self, client):
        # A path-traversal-shaped name is rejected by the name validator
        # before any disk access. (Slashes never reach this route — FastAPI
        # routes them to a different path — so we use a different invalid.)
        assert client.delete("/api/skills/Bad-Name", headers=AUTH).status_code == 400

    def test_delete_cleans_agent_card_references(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "user-skill")
        _write_skill(fake_dirs["skills"], "other")
        card = _write_agent_card(
            fake_dirs["cards"], "MyAgent", ["user-skill", "other"]
        )
        resp = client.delete("/api/skills/user-skill", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["removed_from_agents"] == ["MyAgent"]
        # Card must no longer reference user-skill but must keep 'other'.
        text = card.read_text(encoding="utf-8")
        fm = yaml.safe_load(text.split("---")[1])
        skills = fm["skills"]
        assert all("user-skill" not in s for s in skills)
        assert any("other" in s for s in skills)

    def test_delete_removes_extra_files_in_dir(self, client, fake_dirs):
        _write_skill(fake_dirs["skills"], "user-skill")
        extra = fake_dirs["skills"] / "user-skill" / "references" / "EXTRA.md"
        extra.parent.mkdir()
        extra.write_text("nested\n", encoding="utf-8")
        resp = client.delete("/api/skills/user-skill", headers=AUTH)
        assert resp.status_code == 200
        assert not (fake_dirs["skills"] / "user-skill").exists()


# ============================================================
# H. Path traversal defence (service-level — never reaches HTTP)
# ============================================================


class TestPathTraversal:
    @pytest.mark.parametrize("malicious", [
        "../etc",
        "..",
        "/abs/path",
        "foo/bar",
        "foo\\bar",
    ])
    def test_traversal_attempts_blocked(self, malicious):
        with pytest.raises(svc.SkillValidationError) as excinfo:
            svc._resolve_skill_dir(malicious)
        assert excinfo.value.status_code == 400


# ============================================================
# I. Frontmatter parsing
# ============================================================


class TestFrontmatterParser:
    def test_parses_valid(self):
        fm, body = svc.parse_frontmatter("---\nname: x\ndescription: y\n---\nhello\n")
        assert fm == {"name": "x", "description": "y"}
        assert body.strip() == "hello"

    def test_rejects_missing_block(self):
        with pytest.raises(svc.SkillValidationError):
            svc.parse_frontmatter("no block here")

    def test_rejects_invalid_yaml(self):
        with pytest.raises(svc.SkillValidationError):
            svc.parse_frontmatter("---\nname: [unterminated\n---\nbody\n")

    def test_rejects_non_mapping(self):
        with pytest.raises(svc.SkillValidationError):
            svc.parse_frontmatter("---\n- item\n---\nbody\n")


# ============================================================
# J. Built-in detection caching
# ============================================================


class TestBuiltinDetection:
    def test_manifest_missing_treats_all_as_user(self, fake_dirs, monkeypatch):
        fake_dirs["builtin"].unlink()
        monkeypatch.setattr(svc, "_builtin_cache", None)
        assert svc.is_builtin("audio-reading") is False

    def test_manifest_invalid_yaml_fails_open(self, fake_dirs, monkeypatch):
        fake_dirs["builtin"].write_text("not: valid: yaml: [", encoding="utf-8")
        monkeypatch.setattr(svc, "_builtin_cache", None)
        assert svc.is_builtin("audio-reading") is False

    def test_manifest_changed_invalidates_cache(self, fake_dirs, monkeypatch):
        monkeypatch.setattr(svc, "_builtin_cache", None)
        assert svc.is_builtin("audio-reading") is True
        # Wait for a different mtime tick on slow filesystems.
        time.sleep(0.01)
        fake_dirs["builtin"].write_text("builtin: []\n", encoding="utf-8")
        assert svc.is_builtin("audio-reading") is False


# ============================================================
# L. Runtime sync — regression: edits must propagate to in-memory
#     SkillManifest copies, otherwise /api/agents/{name} keeps returning
#     pre-edit content even after the user reloads the page.
# ============================================================


class _FakeManifest:
    """Stand-in for fast_agent.config.SkillManifest. The real class has more
    fields but only `name` and `body` matter for the runtime-sync hook.
    """
    def __init__(self, name: str, body: str = "", description: str = ""):
        self.name = name
        self.body = body
        self.description = description


class _FakeAgentConfig:
    def __init__(self, manifests):
        self.skill_manifests = list(manifests)


class _FakeFast:
    def __init__(self, agents_dict):
        self.agents = agents_dict


class TestRuntimeSync:
    def test_update_propagates_to_runtime_skill_manifests(self, client, fake_dirs, monkeypatch):
        """Regression: PUT must update the in-memory manifest, not just disk.

        Before this fix, _get_agent_skills() in routes/agents.py read from a
        stale FastAgent runtime cache, so the dashboard showed pre-edit
        content even across page reloads.
        """
        _write_skill(fake_dirs["skills"], "demo", description="Old", body="OLD body.")
        # Build a fake runtime that holds the stale manifest the way FastAgent
        # would after startup.
        stale = _FakeManifest("demo", body="OLD body.", description="Old")
        cfg = _FakeAgentConfig([stale])
        fake_fast = _FakeFast({"AgentX": {"config": cfg}})

        def loader(name):
            assert name == "demo"
            # Simulate get_skills(): re-read from disk and produce a fresh
            # manifest object.
            md = (fake_dirs["skills"] / name / "SKILL.md").read_text(encoding="utf-8")
            from services.skill_service import parse_frontmatter
            fm, body = parse_frontmatter(md)
            return fake_fast, _FakeManifest(name, body=body, description=fm.get("description", ""))

        monkeypatch.setattr(svc, "_runtime_loader", loader)

        new_content = "---\nname: demo\ndescription: New\n---\n\nNEW body.\n"
        resp = client.put(
            "/api/skills/demo",
            json={"content": new_content, "expected_mtime_ns": None},
            headers=AUTH,
        )
        assert resp.status_code == 200
        # The runtime manifest object must now hold the new body.
        assert cfg.skill_manifests[0].body.strip() == "NEW body."
        assert cfg.skill_manifests[0].description == "New"

    def test_delete_strips_manifest_from_runtime(self, client, fake_dirs, monkeypatch):
        _write_skill(fake_dirs["skills"], "to-delete")
        cfg = _FakeAgentConfig([
            _FakeManifest("to-delete"),
            _FakeManifest("user-context"),
        ])
        fake_fast = _FakeFast({"AgentX": {"config": cfg}})
        monkeypatch.setattr(svc, "_runtime_loader", lambda _name: (fake_fast, None))

        resp = client.delete("/api/skills/to-delete", headers=AUTH)
        assert resp.status_code == 200
        names = [m.name for m in cfg.skill_manifests]
        assert "to-delete" not in names
        assert "user-context" in names

    def test_runtime_sync_silent_when_runtime_unavailable(self, client, fake_dirs, monkeypatch):
        """If agent.py isn't importable (e.g. pure unit-test process), save
        must still succeed — runtime sync is best-effort, not a save gate.
        """
        _write_skill(fake_dirs["skills"], "demo")
        monkeypatch.setattr(svc, "_runtime_loader", lambda _name: (None, None))
        new_content = "---\nname: demo\ndescription: ok\n---\n\nbody\n"
        resp = client.put(
            "/api/skills/demo",
            json={"content": new_content, "expected_mtime_ns": None},
            headers=AUTH,
        )
        assert resp.status_code == 200


# ============================================================
# M. Attach / detach skill ↔ agent
# ============================================================


class _StubAgentInstance:
    """Stand-in for a runtime agent instance that records rebuild calls.

    The real fast-agent McpAgent has many more attributes; we only need the
    surface that `rebuild_agent_instruction` calls (set_skill_manifests +
    set_instruction). The stub records what was passed so tests can assert
    the instruction was actually rebuilt with the new manifest list.
    """
    def __init__(self):
        self.skill_manifests_set: list = []
        self.instruction = ""
        self.rebuild_calls: list[list[_FakeManifest]] = []

    def set_skill_manifests(self, manifests):
        self.skill_manifests_set = list(manifests)

    def set_instruction(self, instruction):
        self.instruction = instruction


class _StubAgentApp:
    def __init__(self, instances: dict[str, _StubAgentInstance]):
        self._instances = instances

    def get_agent(self, name):
        return self._instances.get(name)


def _wire_attach_runtime(monkeypatch, fake_dirs, agents: dict[str, list[str]]):
    """Wire up monkeypatched runtime handles so attach/detach tests run
    without needing the real fast-agent app.

    `agents` maps agent_name -> list of currently-attached skill names. Each
    entry becomes a config with skill_manifests + a runtime instance that
    rebuild_agent_instruction would normally update.
    Returns (cfg_map, instance_map, rebuild_calls_recorder).
    """
    cfgs = {n: _FakeAgentConfig([_FakeManifest(s) for s in skills])
            for n, skills in agents.items()}
    instances = {n: _StubAgentInstance() for n in agents}
    fake_fast = _FakeFast({n: {"config": c} for n, c in cfgs.items()})
    fake_state = type("S", (), {"agent_app": _StubAgentApp(instances)})()
    rebuild_calls: list[tuple[str, list]] = []

    async def fake_rebuild(agent_instance, *, skill_manifests=None, **_):
        # Mimic the real function's effect: set manifests + a synthetic
        # instruction so we can assert it actually got rebuilt.
        if skill_manifests is not None:
            agent_instance.set_skill_manifests(skill_manifests)
            names = [getattr(m, "name", "?") for m in skill_manifests]
            agent_instance.set_instruction(f"REBUILT: {','.join(names)}")
        # Find which agent this is to record by name.
        owner = next(n for n, inst in instances.items() if inst is agent_instance)
        rebuild_calls.append((owner, list(skill_manifests or [])))

    def fake_handles():
        return fake_fast, fake_state, fake_rebuild

    def fake_loader(name):
        # Re-read from the test's skills dir to mimic get_skills().
        md = fake_dirs["skills"] / name / "SKILL.md"
        if not md.exists():
            return fake_fast, None
        from services.skill_service import parse_frontmatter
        text = md.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        return fake_fast, _FakeManifest(name, body=body, description=fm.get("description", ""))

    monkeypatch.setattr(svc, "_runtime_handles", fake_handles)
    monkeypatch.setattr(svc, "_runtime_loader", fake_loader)
    return cfgs, instances, rebuild_calls


class TestAttachDetach:
    def test_attach_to_card_agent_persists_yaml_and_rebuilds(self, client, fake_dirs, monkeypatch):
        _write_skill(fake_dirs["skills"], "new-skill")
        _write_agent_card(fake_dirs["cards"], "FinanceAgent", ["finance"])
        cfgs, instances, rebuild_calls = _wire_attach_runtime(
            monkeypatch, fake_dirs, {"FinanceAgent": ["finance"]}
        )

        resp = client.put("/api/skills/new-skill/agents/FinanceAgent", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["persisted"] is True
        # Slim payload: skill_count instead of full manifest list (saves tokens
        # for the LLM and the dashboard refetches separately).
        assert body["skill_count"] == 2

        # YAML must now reference the skill.
        card_text = (fake_dirs["cards"] / "FinanceAgent.md").read_text(encoding="utf-8")
        assert "new-skill" in card_text

        # Runtime: rebuild_agent_instruction was called with both skills.
        assert len(rebuild_calls) == 1
        owner, manifests = rebuild_calls[0]
        assert owner == "FinanceAgent"
        assert {m.name for m in manifests} == {"finance", "new-skill"}
        assert "new-skill" in instances["FinanceAgent"].instruction

    def test_attach_to_code_based_agent_runtime_only(self, client, fake_dirs, monkeypatch):
        _write_skill(fake_dirs["skills"], "new-skill")
        # No agent card on disk → code-based.
        cfgs, instances, rebuild_calls = _wire_attach_runtime(
            monkeypatch, fake_dirs, {"Jarvis": ["user-context"]}
        )

        resp = client.put("/api/skills/new-skill/agents/Jarvis", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["persisted"] is False
        assert body["skill_count"] == 2
        # No card was created.
        assert not (fake_dirs["cards"] / "Jarvis.md").exists()
        # Runtime still got the rebuild call.
        assert len(rebuild_calls) == 1

    def test_attach_unknown_skill_404(self, client, fake_dirs, monkeypatch):
        cfgs, instances, _ = _wire_attach_runtime(monkeypatch, fake_dirs, {"Jarvis": []})
        resp = client.put("/api/skills/missing-skill/agents/Jarvis", headers=AUTH)
        assert resp.status_code == 404

    def test_attach_unknown_agent_404(self, client, fake_dirs, monkeypatch):
        _write_skill(fake_dirs["skills"], "demo")
        _wire_attach_runtime(monkeypatch, fake_dirs, {"Jarvis": []})
        resp = client.put("/api/skills/demo/agents/UnknownAgent", headers=AUTH)
        assert resp.status_code == 404

    def test_attach_already_attached_409(self, client, fake_dirs, monkeypatch):
        _write_skill(fake_dirs["skills"], "demo")
        _wire_attach_runtime(monkeypatch, fake_dirs, {"Jarvis": ["demo"]})
        resp = client.put("/api/skills/demo/agents/Jarvis", headers=AUTH)
        assert resp.status_code == 409

    def test_attach_runtime_failure_rolls_back_yaml(self, client, fake_dirs, monkeypatch):
        _write_skill(fake_dirs["skills"], "demo")
        _write_agent_card(fake_dirs["cards"], "FinanceAgent", ["finance"])
        cfgs, instances, _ = _wire_attach_runtime(
            monkeypatch, fake_dirs, {"FinanceAgent": ["finance"]}
        )
        # Capture the wired-up handles BEFORE swapping in the failing rebuild,
        # otherwise the new handles fn would call back into itself (recursion).
        wired_handles = svc._runtime_handles
        async def boom(*args, **kwargs):
            raise RuntimeError("fast-agent rebuild blew up")
        def handles_with_boom():
            fast_obj, state, _ = wired_handles()
            return fast_obj, state, boom
        monkeypatch.setattr(svc, "_runtime_handles", handles_with_boom)

        original_card = (fake_dirs["cards"] / "FinanceAgent.md").read_bytes()
        resp = client.put("/api/skills/demo/agents/FinanceAgent", headers=AUTH)
        assert resp.status_code == 500
        # YAML must be unchanged after rollback.
        assert (fake_dirs["cards"] / "FinanceAgent.md").read_bytes() == original_card

    def test_detach_from_card_agent_persists_yaml(self, client, fake_dirs, monkeypatch):
        _write_skill(fake_dirs["skills"], "finance")
        _write_skill(fake_dirs["skills"], "research")
        _write_agent_card(fake_dirs["cards"], "FinanceAgent", ["finance", "research"])
        cfgs, instances, rebuild_calls = _wire_attach_runtime(
            monkeypatch, fake_dirs, {"FinanceAgent": ["finance", "research"]}
        )

        resp = client.delete("/api/skills/research/agents/FinanceAgent", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["persisted"] is True
        assert body["skill_count"] == 1

        card_text = (fake_dirs["cards"] / "FinanceAgent.md").read_text(encoding="utf-8")
        assert "research" not in card_text
        assert "finance" in card_text

        assert len(rebuild_calls) == 1
        _owner, manifests = rebuild_calls[0]
        assert {m.name for m in manifests} == {"finance"}

    def test_detach_from_code_based_agent_runtime_only(self, client, fake_dirs, monkeypatch):
        _write_skill(fake_dirs["skills"], "user-context")
        cfgs, instances, _ = _wire_attach_runtime(
            monkeypatch, fake_dirs, {"Jarvis": ["user-context", "delegation-strategy"]}
        )
        resp = client.delete("/api/skills/user-context/agents/Jarvis", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["persisted"] is False

    def test_detach_not_attached_409(self, client, fake_dirs, monkeypatch):
        _write_skill(fake_dirs["skills"], "demo")
        _wire_attach_runtime(monkeypatch, fake_dirs, {"Jarvis": []})
        resp = client.delete("/api/skills/demo/agents/Jarvis", headers=AUTH)
        assert resp.status_code == 409


# ============================================================
# K. Template
# ============================================================


class TestTemplate:
    def test_template_endpoint_returns_valid_skill(self, client):
        resp = client.get("/api/skills/_template", headers=AUTH)
        assert resp.status_code == 200
        content = resp.json()["content"]
        # The template must itself be parseable.
        fm, _ = svc.parse_frontmatter(content)
        assert "name" in fm
        assert "description" in fm
