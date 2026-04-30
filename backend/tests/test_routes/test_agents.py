"""Tests for agents routes — runtime metadata + CRUD + spawn registry parsing."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest
from routes.agent_timeline import _safe_load_activity_data as timeline_safe_load_activity_data


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

class _FakeConfig:
    """Minimal stand-in for AgentConfig with attribute access."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_agent_data(*, config_kwargs=None, child_agents=None, tool_only=False):
    """Build a fake fast.agents entry."""
    cfg = _FakeConfig(**(config_kwargs or {}))
    data = {"config": cfg}
    if child_agents:
        data["child_agents"] = child_agents
    if tool_only:
        data["tool_only"] = True
    return data


@pytest.fixture()
def mock_fast():
    """Patch the `fast` module-level object used in routes.agents."""
    fake_fast = MagicMock()
    fake_fast.agents = {}
    fake_fast._agent_card_sources = {}
    return fake_fast


# ──────────────────────────────────────────────
# _build_agent_dict
# ──────────────────────────────────────────────


class TestBuildAgentDict:
    """Tests for _build_agent_dict() — single agent serialisation."""

    def test_basic_fields(self, mock_fast):
        """Should extract name, description, instruction, model, servers from config."""
        mock_fast.agents = {
            "TestAgent": _make_agent_data(config_kwargs={
                "description": "A test agent",
                "instruction": "Do testing",
                "model": "openai.gpt-4o",
                "servers": ["server-a", "server-b"],
                "default": False,
                "tools": {"server-a": ["tool1"]},
            })
        }
        with patch("routes.agents.fast", mock_fast):
            from routes.agents import _build_agent_dict
            result = _build_agent_dict("TestAgent", mock_fast.agents["TestAgent"])

        assert result["name"] == "TestAgent"
        assert result["description"] == "A test agent"
        assert result["instruction"] == "Do testing"
        assert result["model"] == "openai.gpt-4o"
        assert result["servers"] == ["server-a", "server-b"]
        assert result["tools"] == {"server-a": ["tool1"]}

    def test_builtin_type(self, mock_fast):
        """Agent NOT in _agent_card_sources → type='builtin'."""
        mock_fast.agents = {"Builtin": _make_agent_data(config_kwargs={"instruction": "x"})}
        mock_fast._agent_card_sources = {}  # empty = not from cards

        with patch("routes.agents.fast", mock_fast):
            from routes.agents import _build_agent_dict
            result = _build_agent_dict("Builtin", mock_fast.agents["Builtin"])

        assert result["type"] == "builtin"

    def test_card_type(self, mock_fast):
        """Agent IN _agent_card_sources → type='card'."""
        mock_fast.agents = {"CardAgent": _make_agent_data(config_kwargs={"instruction": "x"})}
        mock_fast._agent_card_sources = {"CardAgent": Path("/cards/CardAgent.md")}

        with patch("routes.agents.fast", mock_fast):
            from routes.agents import _build_agent_dict
            result = _build_agent_dict("CardAgent", mock_fast.agents["CardAgent"])

        assert result["type"] == "card"

    def test_child_agents_and_parent(self, mock_fast):
        """Should populate child_agents list and compute parent_agent."""
        mock_fast.agents = {
            "Parent": _make_agent_data(
                config_kwargs={"instruction": "orchestrate", "default": True},
                child_agents=["ChildA", "ChildB"],
            ),
            "ChildA": _make_agent_data(config_kwargs={"instruction": "do A"}),
            "ChildB": _make_agent_data(config_kwargs={"instruction": "do B"}),
        }

        with patch("routes.agents.fast", mock_fast):
            from routes.agents import _build_agent_dict
            parent_result = _build_agent_dict("Parent", mock_fast.agents["Parent"])
            child_a_result = _build_agent_dict("ChildA", mock_fast.agents["ChildA"])
            child_b_result = _build_agent_dict("ChildB", mock_fast.agents["ChildB"])

        assert parent_result["child_agents"] == ["ChildA", "ChildB"]
        assert parent_result["parent_agent"] is None
        assert child_a_result["parent_agent"] == "Parent"
        assert child_b_result["parent_agent"] == "Parent"
        assert child_a_result["child_agents"] == []

    def test_icon_mapping(self, mock_fast):
        """Known agents get specific icons; unknown agents get 'smart_toy'."""
        mock_fast.agents = {
            "IoTAgent": _make_agent_data(config_kwargs={"instruction": "iot"}),
            "CustomAgent": _make_agent_data(config_kwargs={"instruction": "custom"}),
        }

        with patch("routes.agents.fast", mock_fast):
            from routes.agents import _build_agent_dict
            iot = _build_agent_dict("IoTAgent", mock_fast.agents["IoTAgent"])
            custom = _build_agent_dict("CustomAgent", mock_fast.agents["CustomAgent"])

        assert iot["icon"] == "sensors"
        assert custom["icon"] == "smart_toy"  # default

    def test_missing_config_returns_name_only(self, mock_fast):
        """If agent_data has no config, return minimal dict."""
        mock_fast.agents = {"Bare": {"no_config": True}}

        with patch("routes.agents.fast", mock_fast):
            from routes.agents import _build_agent_dict
            result = _build_agent_dict("Bare", mock_fast.agents["Bare"])

        assert result == {"name": "Bare"}

    def test_default_model_when_none(self, mock_fast):
        """model=None → fallback to _get_default_model() value."""
        mock_fast.agents = {
            "NoModel": _make_agent_data(config_kwargs={"instruction": "x", "model": None})
        }
        with patch("routes.agents.fast", mock_fast), \
             patch("routes.agents._get_default_model", return_value="openai.test-model"):
            from routes.agents import _build_agent_dict
            result = _build_agent_dict("NoModel", mock_fast.agents["NoModel"])

        assert result["model"] == "openai.test-model"


# ──────────────────────────────────────────────
# _build_agents_from_runtime
# ──────────────────────────────────────────────


class TestBuildAgentsFromRuntime:
    """Tests for _build_agents_from_runtime() — full agent list building."""

    def test_returns_all_agents(self, mock_fast):
        """Should return one entry per registered agent."""
        mock_fast.agents = {
            "AgentA": _make_agent_data(config_kwargs={"instruction": "a"}),
            "AgentB": _make_agent_data(config_kwargs={"instruction": "b"}),
            "AgentC": _make_agent_data(config_kwargs={"instruction": "c"}),
        }

        with patch("routes.agents.fast", mock_fast):
            from routes.agents import _build_agents_from_runtime
            agents = _build_agents_from_runtime()

        assert len(agents) == 3

    def test_default_agent_sorted_first(self, mock_fast):
        """Agent with is_default=True should appear first."""
        mock_fast.agents = {
            "Zebra": _make_agent_data(config_kwargs={"instruction": "z", "default": False}),
            "Alpha": _make_agent_data(config_kwargs={"instruction": "a", "default": False}),
            "Jarvis": _make_agent_data(config_kwargs={"instruction": "j", "default": True}),
        }

        with patch("routes.agents.fast", mock_fast):
            from routes.agents import _build_agents_from_runtime
            agents = _build_agents_from_runtime()

        assert agents[0]["name"] == "Jarvis"
        assert agents[0]["is_default"] is True

    def test_skips_agents_without_config(self, mock_fast):
        """Agents with no config key should be skipped."""
        mock_fast.agents = {
            "Good": _make_agent_data(config_kwargs={"instruction": "ok"}),
            "NoConfig": {},  # no config
        }

        with patch("routes.agents.fast", mock_fast):
            from routes.agents import _build_agents_from_runtime
            agents = _build_agents_from_runtime()

        assert len(agents) == 1
        assert agents[0]["name"] == "Good"

    def test_empty_registry(self, mock_fast):
        """Empty fast.agents → empty list."""
        mock_fast.agents = {}

        with patch("routes.agents.fast", mock_fast):
            from routes.agents import _build_agents_from_runtime
            agents = _build_agents_from_runtime()

        assert agents == []


# ──────────────────────────────────────────────
# _is_static_agent
# ──────────────────────────────────────────────


class TestIsStaticAgent:
    """Tests for _is_static_agent() — builtin vs card agent check."""

    def test_builtin_agent(self, mock_fast):
        """Agent in fast.agents but NOT in _agent_card_sources → static (builtin)."""
        mock_fast.agents = {"Jarvis": _make_agent_data(config_kwargs={"instruction": "j"})}
        mock_fast._agent_card_sources = {}

        with patch("routes.agents.fast", mock_fast):
            from routes.agents import _is_static_agent
            assert _is_static_agent("Jarvis") is True

    def test_card_agent(self, mock_fast):
        """Agent in both fast.agents AND _agent_card_sources → NOT static."""
        mock_fast.agents = {"Finance": _make_agent_data(config_kwargs={"instruction": "f"})}
        mock_fast._agent_card_sources = {"Finance": Path("/cards/Finance.md")}

        with patch("routes.agents.fast", mock_fast):
            from routes.agents import _is_static_agent
            assert _is_static_agent("Finance") is False

    def test_unknown_agent(self, mock_fast):
        """Agent not in fast.agents at all → not static."""
        mock_fast.agents = {}

        with patch("routes.agents.fast", mock_fast):
            from routes.agents import _is_static_agent
            assert _is_static_agent("Nonexistent") is False


# ──────────────────────────────────────────────
# Spawn registry parsing (unchanged existing tests)
# ──────────────────────────────────────────────


class TestListAgentsRoute:
    """Tests for spawn registry parsing logic."""

    def test_parse_spawn_registry_valid(self, sample_spawn_registry):
        """Should parse valid spawn_registry.json correctly."""
        registry_data = json.loads(sample_spawn_registry.read_text())

        agents = []
        for run_id, rec in registry_data.items():
            if rec.get("lifecycle") == "oneshot":
                continue
            agents.append({
                "name": rec.get("agent_name", ""),
                "role": rec.get("role", ""),
                "status": rec.get("status", "unknown"),
                "type": "team",
                "run_id": run_id,
            })

        assert len(agents) == 2
        assert agents[0]["name"] == "Linh - PM"
        assert agents[0]["type"] == "team"
        assert agents[1]["status"] == "running"

    def test_parse_empty_registry(self, tmp_path):
        """Should handle empty registry gracefully."""
        empty_file = tmp_path / "spawn_registry.json"
        empty_file.write_text("{}")

        registry_data = json.loads(empty_file.read_text())
        agents = [
            {"name": rec.get("agent_name", ""), "type": "team"}
            for run_id, rec in registry_data.items()
            if rec.get("lifecycle") != "oneshot"
        ]
        assert agents == []

    def test_parse_missing_registry(self, tmp_path):
        """Should not crash if registry file is missing."""
        registry_path = tmp_path / "nonexistent.json"
        try:
            data = json.loads(registry_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}

        assert data == {}

    def test_oneshot_agents_excluded(self, tmp_path):
        """Oneshot agents should be filtered out."""
        registry = {
            "run-001": {"agent_name": "OneShot", "lifecycle": "oneshot", "status": "idle"},
            "run-002": {"agent_name": "Persistent", "lifecycle": "resumable", "status": "idle"},
        }
        reg_file = tmp_path / "registry.json"
        reg_file.write_text(json.dumps(registry))

        data = json.loads(reg_file.read_text())
        agents = [
            rec for run_id, rec in data.items()
            if rec.get("lifecycle") != "oneshot"
        ]
        assert len(agents) == 1
        assert agents[0]["agent_name"] == "Persistent"


class TestAgentIconMapping:
    """Tests for AGENT_ICONS mapping."""

    def test_known_agents_have_icons(self):
        """Known agent names should have specific icon values."""
        from routes.agents import AGENT_ICONS
        assert AGENT_ICONS["Jarvis"] == "smart_toy"
        assert AGENT_ICONS["PersonalAgent"] == "person"
        assert AGENT_ICONS["IoTAgent"] == "sensors"
        assert AGENT_ICONS["MusicAgent"] == "music_note"
        assert AGENT_ICONS["FinanceAgent"] == "trending_up"

    def test_unknown_agent_gets_default(self):
        """Unlisted agents should fall back to 'smart_toy'."""
        from routes.agents import AGENT_ICONS
        icon = AGENT_ICONS.get("RandomAgent", "smart_toy")
        assert icon == "smart_toy"


class TestAgentActivitiesJsonParsing:
    """Regression tests for malformed data_json rows."""

    def test_malformed_data_json_does_not_break_response(self):
        from routes.agents import _safe_load_activity_data

        assert _safe_load_activity_data('{bad json') is None

    def test_valid_data_json_still_parses(self):
        from routes.agents import _safe_load_activity_data

        assert _safe_load_activity_data('{"tool_name": "send_email"}') == {"tool_name": "send_email"}


class TestAgentTimelineJsonParsing:
    """Regression tests for malformed timeline activity data_json rows."""

    def test_malformed_timeline_data_json_does_not_break_response(self):
        assert timeline_safe_load_activity_data('{bad json') is None

    def test_valid_timeline_data_json_still_parses(self):
        assert timeline_safe_load_activity_data('{"tool_name": "send_email"}') == {"tool_name": "send_email"}
