"""Tests for the skill-driven reload helpers in services.team_reload.

Pin the contract: when a skill is updated, we must (a) compute who's
affected without side-effects, and (b) only respawn agents whose role
references that skill — not the whole team.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services import team_reload


def _fake_team(session_id: str, team_name: str, role_to_skills: dict[str, list[str]]):
    """Build a team_sessions row matching the live shape."""
    return {
        "session_id": session_id,
        "team_name": team_name,
        "workspace": "/tmp/ws",
        "project_brief": "x",
        "parent_session_id": "",
        "conversation_id": "",
        "agents": {
            f"{role}-1": {"run_id": f"run-{role}", "role": role, "status": "running"}
            for role in role_to_skills
        },
        "sprint_status": "running",
        "template": {
            "name": "agile-team",
            "orchestrator": "pm",
            "roles": {
                role: {"role_display": role.upper(), "instruction": "", "servers": [], "skills": skills, "model": ""}
                for role, skills in role_to_skills.items()
            },
        },
    }


# ── find_sessions_using_skill ──────────────────────────────────────────────


class TestFindSessionsUsingSkill:
    def test_returns_only_roles_using_skill(self):
        teams = [
            _fake_team("ses-a", "A", {
                "qe": ["qe-workflow", "team-communication"],
                "dev": ["dev-workflow", "team-communication"],
                "pm": ["project-management"],  # doesn't use team-communication
            }),
            _fake_team("ses-b", "B", {
                "ba": ["ba-workflow", "team-communication"],
            }),
            _fake_team("ses-c", "C", {
                "pm": ["project-management"],  # no match anywhere
            }),
        ]
        with patch.object(team_reload, "list_team_sessions", create=True) if False else patch(
            "fast_agent.spawn.team_spawner.list_team_sessions",
            return_value=teams,
        ):
            result = team_reload.find_sessions_using_skill("team-communication")

        assert len(result) == 2
        by_sid = {r["session_id"]: r for r in result}
        assert sorted(by_sid["ses-a"]["roles"]) == ["dev", "qe"]
        assert by_sid["ses-b"]["roles"] == ["ba"]
        assert "ses-c" not in by_sid  # zero matching roles → not included

    def test_unused_skill_returns_empty(self):
        with patch(
            "fast_agent.spawn.team_spawner.list_team_sessions",
            return_value=[_fake_team("ses-a", "A", {"pm": ["x"]})],
        ):
            assert team_reload.find_sessions_using_skill("non-existent") == []


# ── reload_by_skill ────────────────────────────────────────────────────────


class TestReloadBySkill:
    @pytest.mark.asyncio
    async def test_reloads_only_affected_roles(self):
        teams = [
            _fake_team("ses-a", "A", {
                "qe": ["qe-workflow", "team-communication"],
                "dev": ["dev-workflow"],  # not using team-communication
            }),
        ]
        captured: dict[str, list[str]] = {}

        async def fake_reload_roles(*, session_id, roles, edited_by, inject_message):
            captured[session_id] = list(roles)
            return {r: [{"agent_name": f"{r}-1", "killed": True, "resumed": True}] for r in roles}

        with patch(
            "fast_agent.spawn.team_spawner.list_team_sessions",
            return_value=teams,
        ), patch.object(team_reload, "reload_roles", new=AsyncMock(side_effect=fake_reload_roles)):
            result = await team_reload.reload_by_skill("team-communication")

        # qe is the ONLY role using the skill — dev untouched
        assert captured == {"ses-a": ["qe"]}
        assert result["skill"] == "team-communication"
        assert len(result["sessions"]) == 1
        assert result["sessions"][0]["roles"] == ["qe"]

    @pytest.mark.asyncio
    async def test_per_session_error_does_not_abort_others(self):
        teams = [
            _fake_team("ses-a", "A", {"qe": ["team-communication"]}),
            _fake_team("ses-b", "B", {"qe": ["team-communication"]}),
        ]
        call_count = {"n": 0}

        async def flaky(*, session_id, roles, edited_by, inject_message):
            call_count["n"] += 1
            if session_id == "ses-a":
                raise RuntimeError("boom")
            return {"qe": [{"agent_name": "qe-1", "resumed": True}]}

        with patch(
            "fast_agent.spawn.team_spawner.list_team_sessions",
            return_value=teams,
        ), patch.object(team_reload, "reload_roles", new=AsyncMock(side_effect=flaky)):
            result = await team_reload.reload_by_skill("team-communication")

        assert call_count["n"] == 2  # both sessions attempted
        by_sid = {s["session_id"]: s for s in result["sessions"]}
        assert "_error" in by_sid["ses-a"]["results"]
        # ses-b still succeeded
        assert "qe" in by_sid["ses-b"]["results"]
        assert by_sid["ses-b"]["results"]["qe"][0]["resumed"]

    @pytest.mark.asyncio
    async def test_no_match_returns_empty_sessions(self):
        with patch(
            "fast_agent.spawn.team_spawner.list_team_sessions",
            return_value=[_fake_team("ses-a", "A", {"pm": ["other"]})],
        ):
            result = await team_reload.reload_by_skill("nonexistent")
        assert result == {"skill": "nonexistent", "sessions": []}

    @pytest.mark.asyncio
    async def test_custom_inject_message_propagates(self):
        teams = [_fake_team("ses-a", "A", {"qe": ["team-communication"]})]
        captured_msgs: list[str] = []

        async def capture_msg(*, session_id, roles, edited_by, inject_message):
            captured_msgs.append(inject_message)
            return {}

        with patch(
            "fast_agent.spawn.team_spawner.list_team_sessions",
            return_value=teams,
        ), patch.object(team_reload, "reload_roles", new=AsyncMock(side_effect=capture_msg)):
            await team_reload.reload_by_skill(
                "team-communication",
                inject_message="custom heads-up text",
            )
        assert captured_msgs == ["custom heads-up text"]
