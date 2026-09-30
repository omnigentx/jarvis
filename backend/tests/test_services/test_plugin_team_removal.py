"""Stopping a team does not make an unconfirmed transport safe to delete."""

import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from services import shared_state
from services.plugins.targets import team_binding


@pytest.mark.asyncio
async def test_dead_team_binding_can_be_disabled_without_respawning(
    tmp_path, monkeypatch
):
    from services.plugins.dispatch import dispatch
    from services.plugins.package import PluginPackage

    record = {
        "run_id": "old",
        "agent_name": "Child",
        "session_id": "s",
        "status": "completed",
        "pid": 987654321,
    }
    monkeypatch.setattr(
        shared_state, "registry_db", SimpleNamespace(get_all=lambda: {"old": record})
    )
    engine = create_engine("sqlite:///" + str(tmp_path / "db"))
    try:
        assert (
            await dispatch(
                tmp_path,
                PluginPackage("test", None, "portable", "a" * 64, (), {}, (), None),
                team_binding(record),
                engine=engine,
                remove=True,
            )
            is True
        )
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_terminal_status_does_not_prove_process_dead(tmp_path, monkeypatch):
    from services.plugins.dispatch import dispatch
    from services.plugins.lifecycle import PluginStateError
    from services.plugins.package import PluginPackage

    record = {
        "run_id": "old",
        "agent_name": "Child",
        "session_id": "s",
        "status": "completed",
        "pid": os.getpid(),
    }
    monkeypatch.setattr(
        shared_state, "registry_db", SimpleNamespace(get_all=lambda: {"old": record})
    )
    engine = create_engine("sqlite:///" + str(tmp_path / "db"))
    try:
        with pytest.raises(PluginStateError):
            await dispatch(
                tmp_path,
                PluginPackage("test", None, "portable", "a" * 64, (), {}, (), None),
                team_binding(record),
                engine=engine,
                remove=True,
            )
    finally:
        engine.dispose()


def test_dead_idle_registry_row_does_not_shadow_resumed_runtime(monkeypatch):
    from services.plugins.targets import live_team_run

    old = {
        "run_id": "old",
        "agent_name": "Child",
        "session_id": "s",
        "status": "idle",
        "pid": 987654321,
    }
    new = {**old, "run_id": "resumed", "pid": os.getpid()}
    monkeypatch.setattr(
        shared_state,
        "registry_db",
        SimpleNamespace(get_all=lambda: {"old": old, "resumed": new}),
    )
    assert live_team_run(team_binding(new))["run_id"] == "resumed"
