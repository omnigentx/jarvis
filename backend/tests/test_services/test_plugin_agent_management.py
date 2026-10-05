"""Agent plugin management must retain hierarchy and actual runtime ACKs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services.plugins import agent_management as service


def test_static_self_and_jarvis_subordinate_authority(monkeypatch):
    configs = {
        "Jarvis": {"config": SimpleNamespace(servers=["plugin_management"])},
        "Worker": {"config": SimpleNamespace(servers=["plugin_management"])},
        "Other": {"config": SimpleNamespace(servers=[])},
    }
    monkeypatch.setattr(service, "static_agents", lambda: configs)
    assert service.authorized_target("Worker", "", "", None) == ("Worker", None)
    assert service.authorized_target("Jarvis", "", "Worker", None) == ("Worker", None)
    with pytest.raises(PermissionError):
        service.authorized_target("Worker", "", "Jarvis", None)
    with pytest.raises(PermissionError):
        service.authorized_target("Other", "", "", None)


def test_team_hierarchy_is_from_persisted_template(monkeypatch):
    records = {
        "pm-run": {
            "run_id": "pm-run",
            "agent_name": "PM",
            "role": "pm",
            "session_id": "team",
            "status": "idle",
        },
        "dev-run": {
            "run_id": "dev-run",
            "agent_name": "Dev",
            "role": "dev",
            "session_id": "team",
            "status": "idle",
        },
        "qe-run": {
            "run_id": "qe-run",
            "agent_name": "QE",
            "role": "qe",
            "session_id": "team",
            "status": "idle",
        },
    }
    monkeypatch.setattr(service, "team_records", lambda: records)
    monkeypatch.setattr(
        service,
        "team_template",
        lambda _id: {
            "orchestrator": "pm",
            "roles": {
                "pm": {"servers": ["plugin_management"]},
                "dev": {"servers": ["plugin_management"]},
                "qe": {"servers": []},
            },
        },
    )
    assert service.authorized_target("PM", "team", "Dev", None) == ("Dev", "dev-run")
    assert service.authorized_target("Dev", "team", "", None) == ("Dev", "dev-run")
    for caller, target, session in [
        ("Dev", "PM", "team"),
        ("Dev", "QE", "team"),
        ("QE", "QE", "team"),
        ("PM", "Dev", "other"),
    ]:
        with pytest.raises(PermissionError):
            service.authorized_target(caller, session, target, None)
    with pytest.raises(PermissionError):
        service.authorized_target("PM", "team", "Dev", "pm-run")
    records["dev-other"] = {**records["dev-run"], "run_id": "dev-other"}
    with pytest.raises(PermissionError):
        service.authorized_target("PM", "team", "Dev", None)


@pytest.mark.asyncio
async def test_add_source_pending_does_not_activate(monkeypatch):
    monkeypatch.setattr(service, "authorized_target", lambda *_: ("Jarvis", None))
    installer = SimpleNamespace(
        install=AsyncMock(return_value={"status": "needs_source_approval"})
    )
    monkeypatch.setattr(service, "runtime_installer", lambda: installer)
    activate = AsyncMock()
    monkeypatch.setattr(service, "activate_binding", activate)
    result = await service.add(
        caller_agent="Jarvis", repo="openai/plugins", commit="a" * 40
    )
    assert result["status"] == "needs_source_approval"
    activate.assert_not_called()


@pytest.mark.asyncio
async def test_add_reuses_reviewed_lifecycle_and_actual_ready_result(monkeypatch):
    monkeypatch.setattr(service, "authorized_target", lambda *_: ("Dev", "dev-run"))
    installer = SimpleNamespace(
        install=AsyncMock(return_value={"id": "candidate", "status": "needs_approval"})
    )
    monkeypatch.setattr(service, "runtime_installer", lambda: installer)
    monkeypatch.setattr(service, "resolve_target", lambda *_: "binding")
    activate = AsyncMock(
        return_value={
            "id": "candidate",
            "status": "ready",
            "servers": {"private": {"env": {"TOKEN": "secret"}}},
            "skills": [],
        }
    )
    monkeypatch.setattr(service, "activate_binding", activate)
    result = await service.add(
        caller_agent="PM",
        session_id="team",
        target_agent="Dev",
        repo="openai/plugins",
        commit="a" * 40,
    )
    assert result["status"] == "ready"
    assert "secret" not in str(result)
    activate.assert_awaited_once_with(
        "candidate", "binding", installer, requested_by="PM", target_label="Dev"
    )


@pytest.mark.asyncio
async def test_forbidden_target_denied_before_download(monkeypatch):
    def forbidden(*_):
        raise PermissionError("forbidden")

    monkeypatch.setattr(service, "authorized_target", forbidden)
    provider = Mock()
    monkeypatch.setattr(service, "runtime_installer", provider)
    with pytest.raises(PermissionError):
        await service.add(
            caller_agent="Dev",
            target_agent="PM",
            repo="openai/plugins",
            commit="a" * 40,
        )
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_inventory_is_scoped_and_does_not_claim_historical_ready_is_live(
    monkeypatch,
):
    monkeypatch.setattr(service, "authorized_target", lambda *_: ("Dev", "dev-run"))
    monkeypatch.setattr(service, "resolve_target", lambda *_: "my-binding")
    record = {
        "id": "candidate",
        "status": "ready",
        "skills": [],
        "bindings": [
            {"agent": "my-binding", "status": "ready"},
            {"agent": "another-team", "status": "ready"},
        ],
        "servers": {"private": {"env": {"TOKEN": "secret"}}},
    }
    monkeypatch.setattr(
        service,
        "runtime_installer",
        lambda: SimpleNamespace(lifecycle=SimpleNamespace(list=lambda: [record])),
    )
    result = await service.inventory(caller_agent="Dev", session_id="team")
    plugin = result["plugins"][0]
    assert "status" not in plugin
    assert plugin["stored_status"] == "ready"
    assert plugin["bindings"] == [{"agent": "my-binding", "stored_status": "ready"}]
    assert "another-team" not in str(result)
    assert "secret" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", ["resumed-run", None])
async def test_inventory_returns_authoritative_current_identity(monkeypatch, selected):
    monkeypatch.setattr(service, "authorized_target", lambda *_: ("Dev", selected))
    monkeypatch.setattr(service, "resolve_target", lambda *_: "my-binding")
    monkeypatch.setattr(
        service,
        "runtime_installer",
        lambda: SimpleNamespace(lifecycle=SimpleNamespace(list=lambda: [])),
    )
    result = await service.inventory(
        caller_agent="Dev", session_id="team" if selected else ""
    )
    assert result["target_agent"] == "Dev"
    assert result["run_id"] == selected
    assert result["binding"] == "my-binding"
