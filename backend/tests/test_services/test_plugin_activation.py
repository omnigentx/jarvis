"""Immediate activation must acknowledge the live agent, never config only."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.plugins.activation import apply_skills
from services.plugins.package import inspect_package


def package(tmp_path):
    (tmp_path / "plugin.json").write_text('{"name":"review"}')
    path = tmp_path / "skills/review"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review changes\n---\nCheck changes."
    )
    return inspect_package(tmp_path)


@pytest.mark.asyncio
async def test_missing_live_agent_never_acknowledges(tmp_path):
    assert await apply_skills(tmp_path, package(tmp_path), "Jarvis", app=None) is False


@pytest.mark.asyncio
async def test_success_checks_rebuild_result_and_namespaces(tmp_path):
    candidate = package(tmp_path)
    agent = SimpleNamespace(skill_manifests=[])
    app = SimpleNamespace(get_agent=lambda name: agent)
    refresh = AsyncMock(return_value=SimpleNamespace(rebuilt_instruction=True))
    result = await apply_skills(tmp_path, candidate, "Jarvis", app=app, refresh=refresh)
    assert result is True
    manifests = refresh.call_args.kwargs["skill_manifests"]
    assert manifests[0].name == "review:review"
    assert manifests[0].path.is_absolute()
    assert "Check changes" in manifests[0].body


@pytest.mark.asyncio
async def test_noop_refresh_does_not_claim_ready(tmp_path):
    agent = SimpleNamespace(skill_manifests=[])
    app = SimpleNamespace(get_agent=lambda name: agent)
    refresh = AsyncMock(return_value=SimpleNamespace(rebuilt_instruction=False))
    assert not await apply_skills(
        tmp_path, package(tmp_path), "Jarvis", app=app, refresh=refresh
    )


@pytest.mark.asyncio
async def test_reapply_replaces_namespace_preserving_existing_skill(tmp_path):
    candidate = package(tmp_path)
    original = SimpleNamespace(name="builtin")
    prior = SimpleNamespace(name="review:review")
    agent = SimpleNamespace(skill_manifests=[original, prior])
    refresh = AsyncMock(return_value=SimpleNamespace(rebuilt_instruction=True))
    await apply_skills(
        tmp_path,
        candidate,
        "Jarvis",
        app=SimpleNamespace(get_agent=lambda name: agent),
        refresh=refresh,
    )
    result = refresh.call_args.kwargs["skill_manifests"]
    assert result[0] is original
    assert [m.name for m in result] == ["builtin", "review:review"]


@pytest.mark.asyncio
async def test_removal_preserves_unrelated_skills_and_requires_refresh_ack(tmp_path):
    from services.plugins.activation import remove_skills

    builtin = SimpleNamespace(name="builtin")
    agent = SimpleNamespace(
        skill_manifests=[
            builtin,
            SimpleNamespace(
                name="review:review", path=tmp_path / "skills/review/SKILL.md"
            ),
        ]
    )
    refresh = AsyncMock(return_value=SimpleNamespace(rebuilt_instruction=True))
    assert await remove_skills(
        tmp_path,
        package(tmp_path),
        "Jarvis",
        app=SimpleNamespace(get_agent=lambda name: agent),
        refresh=refresh,
    )
    assert refresh.call_args.kwargs["skill_manifests"] == [builtin]


@pytest.mark.asyncio
async def test_removing_old_version_does_not_remove_new_version_namespace(tmp_path):
    from services.plugins.activation import remove_skills

    candidate = package(tmp_path)
    new = SimpleNamespace(
        name="review:review", path=tmp_path / "other-version/skills/review/SKILL.md"
    )
    agent = SimpleNamespace(skill_manifests=[new])
    refresh = AsyncMock(return_value=SimpleNamespace(rebuilt_instruction=True))
    assert await remove_skills(
        tmp_path,
        candidate,
        "Jarvis",
        app=SimpleNamespace(get_agent=lambda name: agent),
        refresh=refresh,
    )
    assert refresh.call_args.kwargs["skill_manifests"] == [new]


@pytest.mark.asyncio
async def test_app_lookup_none_is_negative_ack(tmp_path):
    candidate = package(tmp_path)
    assert (
        await apply_skills(
            tmp_path,
            candidate,
            "Missing",
            app=SimpleNamespace(get_agent=lambda _: None),
        )
        is False
    )
