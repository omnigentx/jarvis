import pytest
from services.plugins.manual_review import issue_review, verify_review
from services.plugins.lifecycle import PluginStateError


def test_review_is_bound_to_content_target_and_policy():
    claims = {
        "id": "sample",
        "digest": "a" * 64,
        "target": "Jarvis",
        "policy_revision": "r1",
    }
    token = issue_review(claims, now=100)
    verify_review(token, claims, now=101)
    for field in claims:
        with pytest.raises(PluginStateError):
            verify_review(token, {**claims, field: "changed"}, now=101)


def test_expired_and_forged_reviews_fail_closed():
    claims = {
        "id": "sample",
        "digest": "a" * 64,
        "target": "Jarvis",
        "policy_revision": "",
    }
    token = issue_review(claims, now=100)
    for invalid, now in [(token, 1000), (token + "x", 101), ("malformed", 101)]:
        with pytest.raises(PluginStateError):
            verify_review(invalid, claims, now=now)


@pytest.mark.asyncio
async def test_manual_activation_integrates_lifecycle_without_agent_gate(
    tmp_path, monkeypatch
):
    from sqlalchemy import create_engine
    from services.plugins.lifecycle import PluginLifecycle
    from services.plugins.installation import PluginInstaller
    from services.plugins import operations
    from unittest.mock import AsyncMock

    root = tmp_path / "source"
    (root / "skills/review").mkdir(parents=True)
    (root / "plugin.json").write_text('{"name":"review"}')
    (root / "skills/review/SKILL.md").write_text(
        "---\nname: review\ndescription: Review code\n---\nReview changes."
    )
    installer = PluginInstaller(
        PluginLifecycle(
            create_engine("sqlite:///" + str(tmp_path / "plugins.db")),
            tmp_path / "cache",
        )
    )
    record = installer.lifecycle.stage(
        root, repo="openai/plugins", commit="a" * 40, subdirectory="plugins/review"
    )
    gate = AsyncMock(return_value=False)
    apply = AsyncMock(return_value=True)
    monkeypatch.setattr(operations, "approve_content", gate)
    monkeypatch.setattr(operations, "dispatch", apply)
    claims = {
        "id": record["id"],
        "digest": record["digest"],
        "target": "Jarvis",
        "policy_revision": "",
    }
    token = issue_review(claims)
    with pytest.raises(PluginStateError):
        await operations.activate_binding(
            record["id"], "OtherAgent", installer, manual_review=token
        )
    apply.assert_not_called()
    result = await operations.activate_binding(
        record["id"], "Jarvis", installer, manual_review=token
    )
    assert result["status"] == "ready"
    gate.assert_not_called()
    apply.assert_awaited_once()
    # The exact same runtime via MCP retains human approval.
    result = await operations.activate_binding(record["id"], "OtherAgent", installer)
    assert result["status"] == "needs_approval"
    gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_http_install_is_immediately_readable_by_real_runtime(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    import httpx
    from fastapi import FastAPI
    from sqlalchemy import create_engine
    from fast_agent.agents.agent_types import AgentConfig
    from fast_agent.agents.mcp_agent import McpAgent
    from fast_agent.context import Context
    from services import shared_state
    from services.plugins import installation, operations
    from services.plugins.installation import PluginInstaller
    from services.plugins.lifecycle import PluginLifecycle
    from routes.plugins import router, get_installer
    from core.auth import verify_api_key
    from unittest.mock import AsyncMock

    installer = PluginInstaller(
        PluginLifecycle(
            create_engine("sqlite:///" + str(tmp_path / "runtime.db")),
            tmp_path / "cache",
        )
    )
    runtime = McpAgent(
        AgentConfig(
            name="Jarvis", instruction="Assist. {{agentSkills}}", servers=[], skills=[]
        ),
        context=Context(no_shell=True),
    )
    monkeypatch.setattr(
        shared_state,
        "agent_app",
        SimpleNamespace(get_agent=lambda name: runtime if name == "Jarvis" else None),
    )
    gate = AsyncMock(
        side_effect=AssertionError("manual flow must not request approval")
    )
    monkeypatch.setattr(operations, "approve_content", gate)

    async def download(repo, commit, subdirectory, destination, allowed):
        (destination / "skills/review").mkdir(parents=True)
        (destination / "plugin.json").write_text('{"name":"reviewed"}')
        (destination / "skills/review/SKILL.md").write_text(
            "---\nname: review\ndescription: Review\n---\nIMMEDIATE_MANUAL_SKILL"
        )
        return destination

    monkeypatch.setattr(installation, "download_package", download)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_installer] = lambda: installer
    app.dependency_overrides[verify_api_key] = lambda: True
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.post(
                "/api/plugins/install",
                json={
                    "repo": "openai/plugins",
                    "commit": "a" * 40,
                    "source_confirmed": True,
                },
            )
            assert result.status_code == 200
            record = result.json()
            assert record["status"] == "needs_approval"
            reviewed = await client.post(
                f"/api/plugins/{record['id']}/manual-review", json={"agent": "Jarvis"}
            )
            assert reviewed.status_code == 200
            activation = await client.post(
                f"/api/plugins/{record['id']}/manual-activate",
                json={
                    "agent": "Jarvis",
                    "review_token": reviewed.json()["review_token"],
                },
            )
            assert activation.status_code == 200
            assert activation.json()["status"] == "ready"
            path = (
                installer.lifecycle.package_path(record["id"])
                / "skills/review/SKILL.md"
            )
            answer = await runtime.call_tool("read_skill", {"path": str(path)})
            assert not answer.isError
            assert "IMMEDIATE_MANUAL_SKILL" in answer.content[0].text
            gate.assert_not_called()
    finally:
        await runtime.shutdown()


def test_workers_share_signing_identity_in_runtime_database(tmp_path):
    from sqlalchemy import create_engine
    from services.plugins.manual_review import _key

    url = "sqlite:///" + str(tmp_path / "keys.db")
    first, second = create_engine(url), create_engine(url)
    assert _key(first) == _key(second)
