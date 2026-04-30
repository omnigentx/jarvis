"""Tests for dynamic agents — reload service and agent card loading."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

from services.dynamic_agents import (
    preload_agent_cards,
    signal_reload_loop,
    AGENT_CARDS_DIR,
)


class TestPreloadAgentCards:
    """Tests for preload_agent_cards function."""

    @pytest.mark.asyncio
    async def test_creates_dir_if_missing(self, tmp_path, monkeypatch):
        """Should create agent_cards dir if it doesn't exist."""
        cards_dir = tmp_path / "agent_cards"
        monkeypatch.setattr("services.dynamic_agents.AGENT_CARDS_DIR", cards_dir)

        mock_app = MagicMock()
        result = await preload_agent_cards(mock_app)

        assert cards_dir.exists()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_if_no_cards(self, tmp_path, monkeypatch):
        """Should return empty list if no .md card files found."""
        cards_dir = tmp_path / "agent_cards"
        cards_dir.mkdir()
        monkeypatch.setattr("services.dynamic_agents.AGENT_CARDS_DIR", cards_dir)

        mock_app = MagicMock()
        result = await preload_agent_cards(mock_app)
        assert result == []

    @pytest.mark.asyncio
    async def test_loads_cards_when_present(self, tmp_path, monkeypatch):
        """Should call agent_app.load_agent_card when .md files exist."""
        cards_dir = tmp_path / "agent_cards"
        cards_dir.mkdir()
        (cards_dir / "test_agent.md").write_text("# Test Agent\nYou are a test.")
        monkeypatch.setattr("services.dynamic_agents.AGENT_CARDS_DIR", cards_dir)

        mock_app = MagicMock()
        mock_app.load_agent_card = AsyncMock(return_value=["TestAgent"])
        result = await preload_agent_cards(mock_app)

        mock_app.load_agent_card.assert_called_once()
        assert result == ["TestAgent"]

    @pytest.mark.asyncio
    async def test_handles_load_error_gracefully(self, tmp_path, monkeypatch):
        """Should catch exceptions and return empty list."""
        cards_dir = tmp_path / "agent_cards"
        cards_dir.mkdir()
        (cards_dir / "broken.md").write_text("bad content")
        monkeypatch.setattr("services.dynamic_agents.AGENT_CARDS_DIR", cards_dir)

        mock_app = MagicMock()
        mock_app.load_agent_card = AsyncMock(side_effect=RuntimeError("broken"))
        result = await preload_agent_cards(mock_app)
        assert result == []
