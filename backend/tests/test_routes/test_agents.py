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
        # tools now ALWAYS comes from the live aggregator (not config). With no
        # state.agent_app in tests we expect an empty dict; see
        # ``test_runtime_tools_*`` below for the populated paths.
        assert result["tools"] == {}

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


# ──────────────────────────────────────────────
# Failed-status surface — built-in agent path
# ──────────────────────────────────────────────


class _StubManifest:
    """Stand-in for ``SkillManifest`` carrying only the fields the route reads."""
    def __init__(self, name, description="", body=""):
        self.name = name
        self.description = description
        self.body = body


class _StubAggregator:
    def __init__(self, attached, configured, server_tool_map):
        self._attached_server_names = attached
        self._configured_server_names = configured
        self._server_to_tool_map = server_tool_map


class _StubAgent:
    def __init__(self, *, skill_manifests=None, aggregator=None, server_status=None):
        self.skill_manifests = skill_manifests or []
        self._aggregator = aggregator
        self.instruction = ""
        self._server_status = server_status or {}

    async def get_server_status(self):
        return self._server_status


class _StubAgentApp:
    def __init__(self, agents):
        self._agents = agents

    def get_agent(self, name):
        return self._agents.get(name)


class _StubNamespacedTool:
    def __init__(self, name, description=""):
        from types import SimpleNamespace
        self.tool = SimpleNamespace(name=name, description=description)
        self.server_name = ""
        self.namespaced_tool_name = name


class TestGetAgentSkills:
    """``_get_agent_skills`` must tag each entry with loaded/failed status."""

    def test_loaded_only(self, mock_fast):
        loaded = [_StubManifest("alpha", "alpha desc", "alpha body")]
        mock_fast.agents = {
            "A": _make_agent_data(config_kwargs={
                "skills": ["alpha"],
                "skill_manifests": loaded,
            })
        }
        stub_app = _StubAgentApp({"A": _StubAgent(skill_manifests=loaded)})
        with patch("routes.agents.fast", mock_fast), \
             patch("routes.agents.state") as st:
            st.agent_app = stub_app
            from routes.agents import _get_agent_skills
            result = _get_agent_skills("A")
        assert [s["name"] for s in result] == ["alpha"]
        assert result[0]["status"] == "loaded"
        assert result[0]["content"] == "alpha body"

    def test_requested_but_missing_marked_failed(self, mock_fast):
        loaded = [_StubManifest("alpha", "alpha desc")]
        mock_fast.agents = {
            "A": _make_agent_data(config_kwargs={
                "skills": ["alpha", "beta-missing"],
                "skill_manifests": loaded,
            })
        }
        stub_app = _StubAgentApp({"A": _StubAgent(skill_manifests=loaded)})
        with patch("routes.agents.fast", mock_fast), \
             patch("routes.agents.state") as st:
            st.agent_app = stub_app
            from routes.agents import _get_agent_skills
            result = _get_agent_skills("A")
        by_name = {s["name"]: s for s in result}
        assert by_name["alpha"]["status"] == "loaded"
        assert by_name["beta-missing"]["status"] == "failed"
        assert by_name["beta-missing"]["content"] == ""

    def test_path_style_request_compares_by_basename(self, mock_fast):
        """Card-based agents list skills as paths (".fast-agent/skills/X"),
        but fast-agent resolves to a manifest whose ``name`` is the basename.
        The diff must compare by basename so loaded skills don't get
        false-positive 'failed' entries when both forms refer to the same
        skill."""
        loaded = [_StubManifest("user-context"), _StubManifest("finance")]
        mock_fast.agents = {
            "FA": _make_agent_data(config_kwargs={
                # Mix path-style and bare-name; both should be recognised.
                "skills": [
                    ".fast-agent/skills/user-context",
                    ".fast-agent/skills/finance",
                    ".fast-agent/skills/missing-skill",
                    "another-missing",
                ],
                "skill_manifests": loaded,
            })
        }
        stub_app = _StubAgentApp({"FA": _StubAgent(skill_manifests=loaded)})
        with patch("routes.agents.fast", mock_fast), \
             patch("routes.agents.state") as st:
            st.agent_app = stub_app
            from routes.agents import _get_agent_skills
            result = _get_agent_skills("FA")
        by_name = {s["name"]: s["status"] for s in result}
        assert by_name == {
            "user-context": "loaded",
            "finance": "loaded",
            "missing-skill": "failed",
            "another-missing": "failed",
        }

    def test_falls_back_to_config_manifests_without_live_instance(self, mock_fast):
        loaded = [_StubManifest("alpha")]
        mock_fast.agents = {
            "A": _make_agent_data(config_kwargs={
                "skills": ["alpha"],
                "skill_manifests": loaded,
            })
        }
        with patch("routes.agents.fast", mock_fast), \
             patch("routes.agents.state") as st:
            st.agent_app = None
            from routes.agents import _get_agent_skills
            result = _get_agent_skills("A")
        assert len(result) == 1
        assert result[0]["status"] == "loaded"


class TestGetRuntimeTools:
    """``_get_runtime_tools`` must surface failed MCP servers as a 'failed' entry."""

    def test_connected_servers_only(self, mock_fast):
        agg = _StubAggregator(
            attached={"alpha"},
            configured={"alpha"},
            server_tool_map={"alpha": [_StubNamespacedTool("ping")]},
        )
        stub_app = _StubAgentApp({"A": _StubAgent(aggregator=agg)})
        with patch("routes.agents.fast", mock_fast), \
             patch("routes.agents.state") as st:
            st.agent_app = stub_app
            from routes.agents import _get_runtime_tools
            tools = _get_runtime_tools("A")
        assert tools["alpha"]["status"] == "connected"
        assert tools["alpha"]["tools"] == [{"name": "ping", "description": ""}]

    def test_includes_failed_servers_with_empty_tools(self, mock_fast):
        agg = _StubAggregator(
            attached={"alpha"},
            configured={"alpha", "beta"},
            server_tool_map={"alpha": [_StubNamespacedTool("ping")]},
        )
        stub_app = _StubAgentApp({"A": _StubAgent(aggregator=agg)})
        with patch("routes.agents.fast", mock_fast), \
             patch("routes.agents.state") as st:
            st.agent_app = stub_app
            from routes.agents import _get_runtime_tools
            tools = _get_runtime_tools("A")
        assert tools["alpha"]["status"] == "connected"
        assert tools["beta"]["status"] == "failed"
        assert tools["beta"]["tools"] == []


class TestEnrichMcpErrorsAsync:
    """``_enrich_mcp_errors_async`` should fill error messages on failed entries."""

    @pytest.mark.asyncio
    async def test_fills_error_from_server_status(self, mock_fast):
        from types import SimpleNamespace
        status = {"beta": SimpleNamespace(error_message="connect timeout")}
        stub_app = _StubAgentApp({"A": _StubAgent(server_status=status)})
        tools = {"beta": {"tools": [], "status": "failed", "error": ""}}
        with patch("routes.agents.fast", mock_fast), \
             patch("routes.agents.state") as st:
            st.agent_app = stub_app
            from routes.agents import _enrich_mcp_errors_async
            await _enrich_mcp_errors_async("A", tools)
        assert tools["beta"]["error"] == "connect timeout"

    @pytest.mark.asyncio
    async def test_skips_when_no_failed_entries(self, mock_fast):
        stub_app = _StubAgentApp({"A": _StubAgent(server_status={})})
        tools = {"alpha": {"tools": [], "status": "connected", "error": ""}}
        with patch("routes.agents.fast", mock_fast), \
             patch("routes.agents.state") as st:
            st.agent_app = stub_app
            from routes.agents import _enrich_mcp_errors_async
            await _enrich_mcp_errors_async("A", tools)
        assert tools["alpha"]["error"] == ""


# ──────────────────────────────────────────────
# Spawn detail — runtime_pending + merge helpers
# ──────────────────────────────────────────────


class TestSpawnAgentDetailRuntimePending:
    """When runtime_config has not arrived yet, the detail dict must surface
    a pending state instead of synthesising data from the team template."""

    def _registry_with(self, record):
        class _DB:
            def find_by_name(self, name):
                return [record]
        return _DB()

    def test_runtime_pending_true_without_runtime_config(self):
        record = {
            "agent_name": "Linh - PM",
            "role": "PM",
            "team_name": "ScrumTeam",
            "original_config": {"skills": ["pm-skill"], "servers": ["github"]},
            "status": "running",
            "started_at": 1000,
        }
        import services.shared_state as _state
        with patch.object(_state, "registry_db", self._registry_with(record)):
            from routes.agents import _build_spawn_agent_detail
            detail = _build_spawn_agent_detail("Linh - PM")
        assert detail["runtime_pending"] is True
        assert detail["skills"] == []
        assert detail["tools"] == {}

    def test_runtime_pending_false_when_runtime_config_present(self):
        record = {
            "agent_name": "Linh - PM",
            "role": "PM",
            "team_name": "ScrumTeam",
            "original_config": {"skills": ["pm-skill", "missing-skill"], "servers": []},
            "runtime_config": {
                "resolved_instruction": "be PM",
                "skills": [{"name": "pm-skill", "description": "pm", "content": "..."}],
                "tools": {"github": [{"name": "search_issues"}]},
            },
            "mcp_status": {
                "servers": {
                    "github": {"is_connected": True, "error": ""},
                    "atlassian": {"is_connected": False, "error": "missing token"},
                },
            },
            "status": "idle",
            "started_at": 1000,
        }
        import services.shared_state as _state
        with patch.object(_state, "registry_db", self._registry_with(record)):
            from routes.agents import _build_spawn_agent_detail
            detail = _build_spawn_agent_detail("Linh - PM")

        assert detail["runtime_pending"] is False
        by_name = {s["name"]: s for s in detail["skills"]}
        assert by_name["pm-skill"]["status"] == "loaded"
        assert by_name["missing-skill"]["status"] == "failed"
        assert detail["tools"]["github"]["status"] == "connected"
        assert detail["tools"]["atlassian"]["status"] == "failed"
        assert detail["tools"]["atlassian"]["error"] == "missing token"


class TestMergeHelpers:
    """Pure-function tests for the merge helpers used by the spawn path."""

    def test_parse_skill_names_handles_string_and_list(self):
        from routes.agents import _parse_skill_names
        assert _parse_skill_names("a, b ,c") == ["a", "b", "c"]
        assert _parse_skill_names(["a", "b"]) == ["a", "b"]
        assert _parse_skill_names([{"name": "x"}, "y"]) == ["x", "y"]
        assert _parse_skill_names(None) == []

    def test_merge_skill_status_tags_failed(self):
        from routes.agents import _merge_skill_status
        loaded = [{"name": "a", "description": "d", "content": "c"}]
        out = _merge_skill_status(loaded, ["a", "b"])
        by_name = {s["name"]: s for s in out}
        assert by_name["a"]["status"] == "loaded"
        assert by_name["b"]["status"] == "failed"

    def test_merge_spawn_tool_status_marks_disconnected_servers(self):
        from routes.agents import _merge_spawn_tool_status
        rt = {"alpha": [{"name": "ping"}]}
        mcp = {
            "alpha": {"is_connected": True, "error": ""},
            "beta": {"is_connected": False, "error": "no route"},
        }
        out = _merge_spawn_tool_status(rt, mcp)
        assert out["alpha"]["status"] == "connected"
        assert out["beta"]["status"] == "failed"
        assert out["beta"]["error"] == "no route"


# ──────────────────────────────────────────────
# Runtime-ready broadcast (spawn_progress_bridge)
# ──────────────────────────────────────────────


class TestRuntimeReadyBroadcast:
    """``_handle_runtime_config`` must broadcast a ``runtime_config_ready``
    event so the dashboard refetches without polling."""

    def test_handle_runtime_config_broadcasts(self):
        from services import spawn_progress_bridge as spb

        broadcasted = []

        class _Stream:
            def broadcast(self, payload):
                broadcasted.append(payload)

        class _Registry:
            def upsert_record(self, *_args, **_kwargs):
                pass

            def bulk_upsert_server_tools(self, *_args, **_kwargs):
                return 0

        bridge = spb.SpawnProgressBridge.__new__(spb.SpawnProgressBridge)
        bridge._registry_db = _Registry()
        bridge._request_id = None
        bridge._pm = None

        with patch.object(spb, "logger", MagicMock()), \
             patch("services.activity_stream.activity_stream_manager", _Stream()):
            bridge._handle_runtime_config(
                "Linh - PM",
                {"resolved_instruction": "x", "skills": [], "tools": {}},
                {"run_id": "run-001"},
            )

        events = [b["event_type"] for b in broadcasted]
        assert "runtime_config_ready" in events
        ev = next(b for b in broadcasted if b["event_type"] == "runtime_config_ready")
        assert ev["agent_name"] == "Linh - PM"
        assert ev["run_id"] == "run-001"
