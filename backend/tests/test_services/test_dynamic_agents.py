"""Tests for the DB-backed dynamic agent loader & rev-poll loop.

These exercise the cross-layer invariant: a row in `agent_definitions`
becomes a registered agent in fast.agents through `load_agent_data`.
Real SQLite (not a mock) — the previous file-based incarnation used
filesystem mocks; that was the wrong shape after the DB migration
because the contract is now "DB row → agent_app sees it" rather than
"file on disk → loader picks it up".
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture()
def defs_db(tmp_path, monkeypatch):
    """Empty SQLite path bound via SPAWN_REGISTRY_DB."""
    db_path = str(tmp_path / "test_dynamic_agents.db")
    monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)
    yield db_path


def _import():
    from services import dynamic_agents
    return dynamic_agents


class TestPreloadDynamicAgents:
    """preload_dynamic_agents reads DB rows and shapes them for load_agent_data."""

    @pytest.mark.asyncio
    async def test_empty_db_returns_empty(self, defs_db):
        mod = _import()
        mock_app = MagicMock()
        mock_app.load_agent_data = AsyncMock(return_value=([], []))

        result = await mod.preload_dynamic_agents(mock_app)

        assert result == []
        # Empty DB → no call to load_agent_data (saves a round-trip).
        mock_app.load_agent_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_loads_definitions_from_db(self, defs_db):
        from services import agent_definitions as defs_svc

        defs_svc.create_definition(
            name="Researcher",
            instruction="research things",
            servers=["serpapi"],
        )

        mod = _import()
        mock_app = MagicMock()
        mock_app.load_agent_data = AsyncMock(return_value=(["Researcher"], ["Researcher"]))

        result = await mod.preload_dynamic_agents(mock_app)

        assert result == ["Researcher"]
        # Definition shape is the YAML-frontmatter dict that load_agent_data expects.
        args, _ = mock_app.load_agent_data.call_args
        defs_arg, parent = args
        assert parent == "Jarvis"
        assert len(defs_arg) == 1
        d = defs_arg[0]
        assert d["name"] == "Researcher"
        assert d["instruction"] == "research things"
        assert d["servers"] == ["serpapi"]
        assert d["use_history"] is True

    @pytest.mark.asyncio
    async def test_load_failure_logged_not_raised(self, defs_db):
        from services import agent_definitions as defs_svc

        defs_svc.create_definition(name="Broken", instruction="x")

        mod = _import()
        mock_app = MagicMock()
        mock_app.load_agent_data = AsyncMock(side_effect=RuntimeError("boom"))

        # Must not raise — server startup must survive bad definitions.
        result = await mod.preload_dynamic_agents(mock_app)
        assert result == []


class TestDbRevPollLoop:
    """db_rev_poll_loop polls services.agent_definitions.get_rev()
    and triggers a reload when the rev advances. The integration here
    asserts the *control flow*, not the SQL — that lives in
    test_agent_definitions."""

    @pytest.mark.asyncio
    async def test_initial_rev_recorded_no_reload(self, defs_db, monkeypatch):
        from services import agent_definitions as defs_svc

        defs_svc.create_definition(name="A", instruction="a")
        # Seed rev > 0 before the loop starts; loop should treat that
        # as the baseline and NOT reload until rev changes again.

        mod = _import()
        monkeypatch.setattr(mod, "POLL_INTERVAL", 0.05)

        mock_app = MagicMock()
        mock_app.load_agent_data = AsyncMock(return_value=([], []))

        task = asyncio.create_task(mod.db_rev_poll_loop(mock_app))
        await asyncio.sleep(0.15)  # let it tick a few times
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # No mutation happened during the loop's life → no reload call.
        mock_app.load_agent_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_fires_when_rev_advances(self, defs_db, monkeypatch):
        from services import agent_definitions as defs_svc

        mod = _import()
        monkeypatch.setattr(mod, "POLL_INTERVAL", 0.05)

        mock_app = MagicMock()
        mock_app.load_agent_data = AsyncMock(return_value=(["A"], ["A"]))

        task = asyncio.create_task(mod.db_rev_poll_loop(mock_app))
        # Insert a new row → rev bumps; the loop should detect on its
        # next tick and call load_agent_data with the row's shape.
        await asyncio.sleep(0.07)
        defs_svc.create_definition(name="A", instruction="hello")
        await asyncio.sleep(0.20)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert mock_app.load_agent_data.await_count >= 1
        args, _ = mock_app.load_agent_data.call_args
        defs_arg, parent = args
        names = [d["name"] for d in defs_arg]
        assert "A" in names
        assert parent == "Jarvis"

    @pytest.mark.asyncio
    async def test_reload_failure_doesnt_advance_last_rev(
        self, defs_db, monkeypatch
    ):
        """If load_agent_data raises during a reload, the loop must
        retry on the next tick — otherwise a transient failure would
        silently drop the operator's change. We assert this by raising
        twice in a row and expecting two await calls."""
        from services import agent_definitions as defs_svc

        mod = _import()
        monkeypatch.setattr(mod, "POLL_INTERVAL", 0.05)

        mock_app = MagicMock()
        mock_app.load_agent_data = AsyncMock(side_effect=RuntimeError("transient"))

        task = asyncio.create_task(mod.db_rev_poll_loop(mock_app))
        await asyncio.sleep(0.05)
        defs_svc.create_definition(name="A", instruction="x")
        await asyncio.sleep(0.25)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Two ticks should have seen the same rev change and retried.
        assert mock_app.load_agent_data.await_count >= 2
