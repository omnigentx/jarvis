"""Integration tests for skill_service against the REAL fast-agent runtime.

These tests bypass mocks and exercise the actual `rebuild_agent_instruction`
function from fast-agent. The point is to catch the kind of bug e2e mock
tests miss: where the surface API works (mocks return the expected payload)
but the real cross-layer invariant is broken (the agent's cached
`instruction` string never picks up the new skill body).

These tests are fast — they don't spin up the full FastAgent app, just the
narrow surface that `rebuild_agent_instruction` needs (a `McpInstructionCapable`
agent stub from fast-agent's own test suite shape).
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth as core_auth
from services import skill_service as svc
from routes import skills as skill_routes

# Import the REAL fast-agent types we want to integrate against.
from fast_agent.skills.registry import SkillManifest
from fast_agent.core.instruction_refresh import (
    McpInstructionCapable,
    rebuild_agent_instruction,
)


_KEY = "skill-integ-test-key"
AUTH = {"Authorization": f"Bearer {_KEY}"}


@pytest.fixture(autouse=True)
def _set_master_key(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", _KEY)
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", _KEY)


# ----- Real-protocol agent stub (mirrors fast-agent's own test stub) -----


class _StubAggregator:
    async def get_server_instructions(self):
        return {}


class _RealProtocolAgent:
    """An agent stub that satisfies McpInstructionCapable. fast-agent's own
    rebuild_agent_instruction is exercised against this same shape in
    fast-agent/tests/.../test_instruction_refresh.py — so if our pipeline
    works for this stub, it works for the real McpAgent.
    """
    def __init__(self, template: str):
        self._instruction = template
        self._instruction_template = template
        self._instruction_context: dict[str, str] = {}
        self._skill_manifests: list[SkillManifest] = []
        self._skill_registry = None
        self._aggregator = _StubAggregator()
        self._skill_read_tool_name = "read_skill"

    @property
    def instruction(self) -> str:
        return self._instruction

    def set_instruction(self, instruction: str) -> None:
        self._instruction = instruction

    @property
    def instruction_template(self) -> str:
        return self._instruction_template

    @property
    def instruction_context(self) -> dict[str, str]:
        return self._instruction_context

    @property
    def aggregator(self):
        return self._aggregator

    @property
    def skill_manifests(self):
        return self._skill_manifests

    @property
    def skill_registry(self):
        return self._skill_registry

    @skill_registry.setter
    def skill_registry(self, value):
        self._skill_registry = value

    def set_skill_manifests(self, manifests):
        self._skill_manifests = list(manifests)

    def set_instruction_context(self, ctx):
        self._instruction_context.update(ctx)

    @property
    def has_filesystem_runtime(self) -> bool:
        return False

    @property
    def skill_read_tool_name(self) -> str:
        return self._skill_read_tool_name


# Sanity: must satisfy the protocol. If this fails, fast-agent's contract
# changed and we'd silently lose coverage — fail loudly here instead.
assert isinstance(_RealProtocolAgent("x"), McpInstructionCapable), (
    "fast-agent McpInstructionCapable contract changed — update _RealProtocolAgent"
)


# ----- Test fixtures ------------------------------------------------------


def _write_skill(skills_dir: Path, name: str, *, description="Demo", body="Body."):
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


def _make_manifest(name: str, body: str, description: str = "ok") -> SkillManifest:
    """Build a real SkillManifest dataclass instance."""
    return SkillManifest(
        name=name,
        description=description,
        body=body,
        path=Path(f"/tmp/{name}/SKILL.md"),
    )


@pytest.fixture()
def fake_dirs(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    cards_dir = tmp_path / "agent_cards"
    skills_dir.mkdir()
    cards_dir.mkdir()
    builtin = skills_dir / "_builtin.yaml"
    builtin.write_text("builtin: []\n", encoding="utf-8")
    monkeypatch.setattr(svc, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(svc, "AGENT_CARDS_DIR", cards_dir)
    monkeypatch.setattr(svc, "BUILTIN_MANIFEST", builtin)
    monkeypatch.setattr(svc, "_builtin_cache", None)
    monkeypatch.setattr(svc, "_builtin_mtime_ns", 0)
    # Default: no runtime — tests that need one wire it explicitly.
    monkeypatch.setattr(svc, "_runtime_handles", lambda: (None, None, None))
    return {"skills": skills_dir, "cards": cards_dir}


@pytest.fixture()
def client(fake_dirs):
    app = FastAPI()
    app.include_router(skill_routes.router)
    return TestClient(app)


# ----- Real-runtime test wiring -------------------------------------------


def _wire_real_runtime(monkeypatch, agents_with_skills: dict[str, list[SkillManifest]]):
    """Wire skill_service so _runtime_handles() returns:
    - a fake `fast` with the listed agents
    - a fake `state` whose agent_app.get_agent returns _RealProtocolAgent instances
    - the REAL `rebuild_agent_instruction` from fast-agent
    """
    cfgs: dict[str, _AgentCfg] = {n: _AgentCfg(skills) for n, skills in agents_with_skills.items()}
    instances: dict[str, _RealProtocolAgent] = {
        n: _RealProtocolAgent(template=f"{n} template:\n\n{{{{agentSkills}}}}\n")
        for n in agents_with_skills
    }
    # Pre-seed each agent's skill_manifests with its starting list, mimicking
    # what FastAgent does at boot.
    for name, ag in instances.items():
        ag.set_skill_manifests(agents_with_skills[name])

    fake_fast = _Fast({n: {"config": cfgs[n]} for n in agents_with_skills})
    fake_state = type("S", (), {"agent_app": _AgentApp(instances)})()

    def fake_handles():
        return fake_fast, fake_state, rebuild_agent_instruction

    def fake_loader(name):
        # Re-read from the test skills dir (used by attach for fresh manifest).
        md = svc.SKILLS_DIR / name / "SKILL.md"
        if not md.exists():
            return fake_fast, None
        text = md.read_text(encoding="utf-8")
        fm, body = svc.parse_frontmatter(text)
        return fake_fast, _make_manifest(name, body=body, description=fm.get("description", ""))

    monkeypatch.setattr(svc, "_runtime_handles", fake_handles)
    monkeypatch.setattr(svc, "_runtime_loader", fake_loader)
    return cfgs, instances


class _AgentCfg:
    def __init__(self, manifests):
        self.skill_manifests = list(manifests)


class _Fast:
    def __init__(self, agents):
        self.agents = agents


class _AgentApp:
    def __init__(self, instances):
        self._instances = instances

    def get_agent(self, name):
        return self._instances.get(name)


# ============================================================
# Real integration tests
# ============================================================
#
# IMPORTANT — what fast-agent actually puts in the instruction:
#   <skill>
#     <name>my-skill</name>
#     <description>...</description>
#     <location>/abs/path/to/SKILL.md</location>
#   </skill>
#
# The skill BODY is NOT in the system prompt. The LLM calls a `read_skill`
# tool that re-reads SKILL.md from disk on demand → body content is always
# fresh from disk after a PUT. So content-edit assertions must check that
# the DESCRIPTION (which IS in the instruction) updates; body changes are
# implicit (the disk file is what matters and the LLM reads it lazily).


class TestRealRuntimeIntegration:
    """Exercise the real fast-agent rebuild_agent_instruction against a
    McpInstructionCapable-conforming stub. These prove cross-layer
    invariants that mock e2e can't catch:

    - The skill LIST in the live agent's system prompt actually changes
      after attach/detach (rebuild_agent_instruction ran and wired the new
      manifests into the rendered template).
    - A description edit propagates into the rendered instruction.
    - Disk → runtime sync is real, not just config-table mutation.
    """

    def test_edit_description_updates_agent_dot_instruction(self, client, fake_dirs, monkeypatch):
        """Regression for the bug the user reported: after PUT /api/skills/{name},
        the live agent's `instruction` (used as next LLM turn's system prompt)
        must reflect the new description — not stay frozen at boot-time content.
        """
        _write_skill(fake_dirs["skills"], "demo", description="OLD DESCRIPTION")
        starting = _make_manifest("demo", body="b", description="OLD DESCRIPTION")
        cfgs, instances = _wire_real_runtime(monkeypatch, {"AgentX": [starting]})

        # Boot-state instruction has OLD DESCRIPTION baked in.
        import asyncio
        asyncio.run(rebuild_agent_instruction(
            instances["AgentX"], skill_manifests=[starting]
        ))
        assert "OLD DESCRIPTION" in instances["AgentX"].instruction

        # PUT a new description on disk.
        new_content = dedent("""\
        ---
        name: demo
        description: NEW DESCRIPTION
        ---

        Body unchanged.
        """)
        resp = client.put(
            "/api/skills/demo",
            json={"content": new_content, "expected_mtime_ns": None},
            headers=AUTH,
        )
        assert resp.status_code == 200

        # The critical assertion: the live agent's rendered instruction
        # now carries the new description.
        assert "NEW DESCRIPTION" in instances["AgentX"].instruction
        assert "OLD DESCRIPTION" not in instances["AgentX"].instruction

    def test_attach_makes_skill_appear_in_agent_dot_instruction(self, client, fake_dirs, monkeypatch):
        """After attach, the live agent's system prompt must list the new skill."""
        _write_skill(fake_dirs["skills"], "fresh", description="A fresh skill")
        cfgs, instances = _wire_real_runtime(monkeypatch, {"AgentX": []})

        import asyncio
        asyncio.run(rebuild_agent_instruction(
            instances["AgentX"], skill_manifests=[]
        ))
        assert "<name>fresh</name>" not in instances["AgentX"].instruction

        resp = client.put("/api/skills/fresh/agents/AgentX", headers=AUTH)
        assert resp.status_code == 200

        # Skill name + description now appear in the rendered instruction.
        assert "<name>fresh</name>" in instances["AgentX"].instruction
        assert "A fresh skill" in instances["AgentX"].instruction

    def test_detach_removes_skill_from_agent_dot_instruction(self, client, fake_dirs, monkeypatch):
        _write_skill(fake_dirs["skills"], "to-remove", description="Going away")
        starting = _make_manifest("to-remove", body="b", description="Going away")
        cfgs, instances = _wire_real_runtime(monkeypatch, {"AgentX": [starting]})

        import asyncio
        asyncio.run(rebuild_agent_instruction(
            instances["AgentX"], skill_manifests=[starting]
        ))
        assert "<name>to-remove</name>" in instances["AgentX"].instruction

        resp = client.delete("/api/skills/to-remove/agents/AgentX", headers=AUTH)
        assert resp.status_code == 200

        assert "<name>to-remove</name>" not in instances["AgentX"].instruction

    def test_used_by_reflects_runtime_attach_to_code_based_agent(self, client, fake_dirs, monkeypatch):
        """Regression: after attaching to a code-based agent (no card file,
        no agent.py edit), the live state must surface in /api/skills'
        used_by — otherwise the dashboard keeps showing 'Not attached'
        even though the agent's runtime instruction now references the skill.
        """
        _write_skill(fake_dirs["skills"], "fresh", description="Fresh skill")
        # Wire a code-based agent: no card on disk, just runtime presence.
        cfgs, instances = _wire_real_runtime(monkeypatch, {"Jarvis": []})

        # Pre-attach: list_skills used_by must be empty.
        resp = client.get("/api/skills", headers=AUTH).json()
        skill = next(s for s in resp["skills"] if s["name"] == "fresh")
        assert skill["used_by"] == []

        # Attach.
        resp = client.put("/api/skills/fresh/agents/Jarvis", headers=AUTH)
        assert resp.status_code == 200

        # Post-attach: list_skills used_by must include Jarvis even though
        # nothing on disk references the skill.
        resp = client.get("/api/skills", headers=AUTH).json()
        skill = next(s for s in resp["skills"] if s["name"] == "fresh")
        assert skill["used_by"] == ["Jarvis"]

    def test_delete_skill_strips_it_from_agent_dot_instruction(self, client, fake_dirs, monkeypatch):
        """Deleting a skill globally also strips it from any live agent's
        rendered instruction (no stranded references in next LLM turn).
        """
        _write_skill(fake_dirs["skills"], "doomed", description="Doomed skill")
        starting = _make_manifest("doomed", body="b", description="Doomed skill")
        cfgs, instances = _wire_real_runtime(monkeypatch, {"AgentX": [starting]})

        import asyncio
        asyncio.run(rebuild_agent_instruction(
            instances["AgentX"], skill_manifests=[starting]
        ))
        assert "<name>doomed</name>" in instances["AgentX"].instruction

        resp = client.delete("/api/skills/doomed", headers=AUTH)
        assert resp.status_code == 200

        assert "<name>doomed</name>" not in instances["AgentX"].instruction
