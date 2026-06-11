"""Tests for multi-agent session routing in session_service.

Covers the primary-agent resolution helper, list_sessions + agent_name
exposure, get_display_history agent_name routing, and tool-activity
filtering — the four surfaces touched by the multi-agent session fix.
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services import session_service
from services.session_service import (
    PRIMARY_AGENT_META_KEY,
    SessionService,
    _migrate_legacy_primary_agent,
    _resolve_primary_agent,
)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def _make_session(metadata, directory=None):
    """Build a duck-typed Session with enough surface for the helpers."""
    info = SimpleNamespace(metadata=dict(metadata))
    return SimpleNamespace(info=info, directory=directory)


# ──────────────────────────────────────────────
# _resolve_primary_agent
# ──────────────────────────────────────────────


class TestResolvePrimaryAgent:
    """Resolver only reads metadata — no fallback, no inference."""

    def test_returns_none_for_none_session(self):
        assert _resolve_primary_agent(None) is None

    def test_returns_none_when_metadata_missing(self, caplog):
        session = _make_session({})
        session.info.name = "s-empty"
        with caplog.at_level("WARNING"):
            assert _resolve_primary_agent(session) is None
        # Warning log is essential — it's the debugging hook.
        assert any("Primary agent missing" in r.message for r in caplog.records)

    def test_reads_primary_agent_key(self):
        session = _make_session({PRIMARY_AGENT_META_KEY: "IoTAgent"})
        assert _resolve_primary_agent(session) == "IoTAgent"

    def test_ignores_empty_primary_agent(self):
        session = _make_session({PRIMARY_AGENT_META_KEY: ""})
        session.info.name = "s-blank"
        assert _resolve_primary_agent(session) is None

    def test_does_not_fall_back_to_history_map(self, tmp_path):
        """Even with a populated last_history_by_agent, no inference happens."""
        jarvis = tmp_path / "history_Jarvis.json"
        jarvis.write_text("{}")
        session = _make_session(
            {"last_history_by_agent": {"Jarvis": "history_Jarvis.json"}},
            directory=tmp_path,
        )
        session.info.name = "s-legacy"
        assert _resolve_primary_agent(session) is None


class TestMigrateLegacyPrimaryAgent:
    """Startup migration stamps primary_agent onto legacy sessions."""

    def _fake_manager(self, sessions):
        """Return a fake SessionManager exposing list_sessions + get_session."""
        manager = MagicMock()
        manager.list_sessions.return_value = [s.info for s in sessions]
        by_name = {s.info.name: s for s in sessions}
        manager.get_session.side_effect = lambda name: by_name.get(name)
        return manager

    def _session(self, session_id, metadata, directory=None):
        info = SimpleNamespace(name=session_id, metadata=dict(metadata))
        session = SimpleNamespace(info=info, directory=directory)
        session._save_metadata = MagicMock()
        return session

    def test_stamps_legacy_session_with_newest_agent(self, tmp_path, caplog):
        jarvis = tmp_path / "history_Jarvis.json"
        iot = tmp_path / "history_IoTAgent.json"
        jarvis.write_text("{}")
        time.sleep(0.05)
        iot.write_text("{}")

        session = self._session(
            "s-legacy",
            {
                "last_history_by_agent": {
                    "Jarvis": "history_Jarvis.json",
                    "IoTAgent": "history_IoTAgent.json",
                }
            },
            directory=tmp_path,
        )
        manager = self._fake_manager([session])

        with caplog.at_level("INFO"):
            migrated = _migrate_legacy_primary_agent(manager)

        assert migrated == 1
        assert session.info.metadata[PRIMARY_AGENT_META_KEY] == "IoTAgent"
        session._save_metadata.assert_called_once()
        assert any("stamped primary_agent=IoTAgent" in r.message for r in caplog.records)

    def test_skips_already_stamped(self, tmp_path):
        session = self._session(
            "s-done",
            {
                PRIMARY_AGENT_META_KEY: "Jarvis",
                "last_history_by_agent": {"Jarvis": "history_Jarvis.json"},
            },
            directory=tmp_path,
        )
        manager = self._fake_manager([session])
        assert _migrate_legacy_primary_agent(manager) == 0
        session._save_metadata.assert_not_called()

    def test_skips_session_without_history(self, caplog):
        session = self._session("s-empty", {})
        manager = self._fake_manager([session])
        with caplog.at_level("INFO"):
            assert _migrate_legacy_primary_agent(manager) == 0
        assert any("no history to infer" in r.message for r in caplog.records)

    def test_skips_when_history_files_missing(self, tmp_path, caplog):
        """Map points to files that never landed — don't stamp, warn loudly."""
        session = self._session(
            "s-broken",
            {"last_history_by_agent": {"Ghost": "history_Ghost.json"}},
            directory=tmp_path,
        )
        manager = self._fake_manager([session])
        with caplog.at_level("WARNING"):
            assert _migrate_legacy_primary_agent(manager) == 0
        assert any("no readable history files" in r.message for r in caplog.records)
        session._save_metadata.assert_not_called()


# ──────────────────────────────────────────────
# list_sessions
# ──────────────────────────────────────────────


def _fake_session_info(session_id, metadata, created_ts=1000.0, updated_ts=2000.0):
    """Build a minimal SessionInfo-like stub for list_sessions()."""
    info = SimpleNamespace(
        name=session_id,
        metadata=dict(metadata),
        created_at=SimpleNamespace(timestamp=lambda: created_ts),
        last_activity=SimpleNamespace(timestamp=lambda: updated_ts),
    )
    return info


class TestListSessions:
    def test_skips_sessions_with_no_agent(self):
        """Empty session (created but never sent) shouldn't appear."""
        svc = SessionService()
        empty_info = _fake_session_info("s-empty", {})
        empty_session = _make_session({})

        svc._manager = MagicMock()
        svc._manager.list_sessions.return_value = [empty_info]
        svc._manager.get_session.return_value = empty_session

        assert svc.list_sessions() == {"items": [], "total": 0}

    def test_includes_jarvis_session(self, tmp_path):
        """Classic Jarvis session renders with agent_name=Jarvis."""
        history_file = tmp_path / "history_Jarvis.json"
        history_file.write_text(json.dumps({"messages": []}))

        svc = SessionService()
        info = _fake_session_info(
            "s-jarvis",
            {
                PRIMARY_AGENT_META_KEY: "Jarvis",
                "title": "Hello",
                "last_history_by_agent": {"Jarvis": "history_Jarvis.json"},
            },
        )
        session = _make_session(info.metadata, directory=tmp_path)
        # latest_history_path is called on session, provide it
        session.latest_history_path = lambda agent: history_file

        svc._manager = MagicMock()
        svc._manager.list_sessions.return_value = [info]
        svc._manager.get_session.return_value = session

        result = svc.list_sessions()
        assert result["total"] == 1
        assert len(result["items"]) == 1
        assert result["items"][0]["id"] == "s-jarvis"
        assert result["items"][0]["agent_name"] == "Jarvis"
        assert result["items"][0]["title"] == "Hello"

    def test_hides_legacy_session_with_no_primary_agent(self, tmp_path, caplog):
        """Legacy session (no primary_agent key) is hidden from the list.

        This is the new strict-resolve behavior: without an explicit
        metadata stamp the session won't render. The startup migration is
        what fixes this for real — see TestMigrateLegacyPrimaryAgent. Here
        we assert the in-memory resolve path is strict and logs loudly.
        """
        iot_file = tmp_path / "history_IoTAgent.json"
        iot_file.write_text(json.dumps({"messages": []}))

        svc = SessionService()
        info = _fake_session_info(
            "s-iot",
            {
                # no PRIMARY_AGENT_META_KEY → legacy, un-migrated session
                "last_history_by_agent": {"IoTAgent": "history_IoTAgent.json"},
            },
        )
        session = _make_session(info.metadata, directory=tmp_path)
        session.info.name = "s-iot"
        session.latest_history_path = lambda agent: iot_file if agent == "IoTAgent" else None

        svc._manager = MagicMock()
        svc._manager.list_sessions.return_value = [info]
        svc._manager.get_session.return_value = session

        with caplog.at_level("WARNING"):
            result = svc.list_sessions()
        assert result["items"] == []
        assert any("Primary agent missing" in r.message for r in caplog.records)


# ──────────────────────────────────────────────
# get_display_history
# ──────────────────────────────────────────────


class TestGetDisplayHistory:
    def test_returns_empty_for_unknown_session(self):
        svc = SessionService()
        svc._manager = MagicMock()
        svc._manager.get_session.return_value = None
        assert svc.get_display_history("missing") == []

    def test_returns_empty_when_no_agent_resolvable(self):
        """Session exists but has no primary agent and no history map."""
        svc = SessionService()
        session = _make_session({})
        svc._manager = MagicMock()
        svc._manager.get_session.return_value = session
        assert svc.get_display_history("s-empty") == []

    def test_uses_explicit_agent_name_over_primary(self, tmp_path):
        """Explicit agent_name argument routes to that agent's history file."""
        iot_file = tmp_path / "history_IoTAgent.json"
        iot_file.write_text(
            json.dumps(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        },
                        {
                            "role": "assistant",
                            "stop_reason": "endTurn",
                            "content": [{"type": "text", "text": "hello from iot"}],
                        },
                    ]
                }
            )
        )

        svc = SessionService()
        session = _make_session(
            {PRIMARY_AGENT_META_KEY: "Jarvis"},  # primary says Jarvis
            directory=tmp_path,
        )

        def _path(agent):
            return iot_file if agent == "IoTAgent" else None

        session.latest_history_path = _path
        svc._manager = MagicMock()
        svc._manager.get_session.return_value = session

        # Override to IoTAgent — should read iot_file despite primary=Jarvis
        with patch.object(svc, "_get_tool_activities", return_value=[]):
            result = svc.get_display_history("s-x", agent_name="IoTAgent")
        assert [m["content"] for m in result] == ["hi", "hello from iot"]


# ──────────────────────────────────────────────
# _get_tool_activities agent_name filter
# ──────────────────────────────────────────────


class TestEnsureSession:
    """ensure_session must always return a real backend session id."""

    def test_returns_existing_id_when_session_found(self):
        svc = SessionService()
        svc._manager = MagicMock()
        svc._manager.get_session.return_value = MagicMock()  # exists
        assert svc.ensure_session("existing-id") == "existing-id"
        svc._manager.create_session.assert_not_called()

    def test_creates_new_session_when_id_unknown(self):
        """Client-generated UUID that backend doesn't know → new session."""
        svc = SessionService()
        svc._manager = MagicMock()
        svc._manager.get_session.return_value = None  # miss
        fresh = SimpleNamespace(info=SimpleNamespace(name="20260418-xyz"))
        svc._manager.create_session.return_value = fresh
        assert svc.ensure_session("client-uuid-that-is-invalid") == "20260418-xyz"
        svc._manager.create_session.assert_called_once()

    def test_creates_new_session_when_id_none(self):
        svc = SessionService()
        svc._manager = MagicMock()
        fresh = SimpleNamespace(info=SimpleNamespace(name="20260418-abc"))
        svc._manager.create_session.return_value = fresh
        assert svc.ensure_session(None) == "20260418-abc"
        svc._manager.get_session.assert_not_called()


class TestToolActivityFilter:
    def test_filter_passes_agent_name_to_query(self):
        """When agent_name provided, query filters by it (plus NULL fallback)."""
        svc = SessionService()

        captured = {}

        class FakeQuery:
            def __init__(self):
                self.filters = []

            def filter(self, *args):
                self.filters.append(args)
                return self

            def order_by(self, *args):
                captured["filters"] = self.filters
                return self

            def all(self):
                return []

        fake_db = MagicMock()
        fake_db.query.return_value = FakeQuery()
        fake_db.close = MagicMock()

        with patch("core.database.get_db_session", return_value=fake_db):
            svc._get_tool_activities("s-1", "IoTAgent")

        # We should have at least 2 filter() calls: session+event filter,
        # and the agent_name OR filter.
        assert len(captured["filters"]) >= 2

    def test_no_filter_when_agent_name_missing(self):
        svc = SessionService()

        class FakeQuery:
            def __init__(self):
                self.filters = []

            def filter(self, *args):
                self.filters.append(args)
                return self

            def order_by(self, *args):
                self.ordered = True
                return self

            def all(self):
                return []

        q = FakeQuery()
        fake_db = MagicMock()
        fake_db.query.return_value = q

        with patch("core.database.get_db_session", return_value=fake_db):
            svc._get_tool_activities("s-1", None)

        # Only the initial session+event filter, no agent_name OR filter.
        assert len(q.filters) == 1


# ──────────────────────────────────────────────
# _merge_tool_activities: run_id-keyed alignment
# ──────────────────────────────────────────────


def _call(run_id, tool_name, args=None, ts=0.0):
    return {
        "event_type": "tool_call",
        "run_id": run_id,
        "agent_name": "Jarvis",
        "data": {"tools": [{"name": tool_name, "args": args or {}}]},
        "created_at": ts,
    }


def _result(run_id, duration_ms=100, preview="ok", ts=0.0):
    return {
        "event_type": "tool_result",
        "run_id": run_id,
        "agent_name": "Jarvis",
        "data": {"duration_ms": duration_ms, "result_preview": preview},
        "created_at": ts,
    }


class TestMergeToolActivities:
    """Each turn's tools must land on that turn's assistant message, not
    be glued onto the last one."""

    def test_per_turn_tools_land_on_matching_assistant(self):
        """Two turns, two run_ids → each assistant message gets its own tools."""
        svc = SessionService()
        messages = [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": "22°C"},
            {"role": "user", "content": "crawl tru tiên"},
            {"role": "assistant", "content": "Đã bắt đầu"},
        ]
        activities = [
            _call("run-1", "ResearchAgent", {"q": "weather"}, ts=1.0),
            _result("run-1", duration_ms=500, preview="22°C", ts=1.5),
            _call("run-2", "CrawlStoriesAgent", {"q": "tru tiên"}, ts=2.0),
            _result("run-2", duration_ms=300, preview="started", ts=2.5),
        ]
        ordered = ["run-1", "run-2"]

        result = svc._merge_tool_activities(messages, activities, ordered)

        assert result[1]["tool_calls"][0]["tool"] == "ResearchAgent"
        assert result[1]["tool_calls"][0]["args"] == {"q": "weather"}
        assert result[3]["tool_calls"][0]["tool"] == "CrawlStoriesAgent"
        assert result[3]["tool_calls"][0]["args"] == {"q": "tru tiên"}

    def test_tools_not_merged_into_last_message_across_turns(self):
        """Regression: previous implementation lumped every turn's tools
        onto the last assistant, mixing previous turns' bubbles with the
        current one. Each assistant must only see its own run_id's tools."""
        svc = SessionService()
        messages = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "A"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "B"},
        ]
        activities = [
            _call("run-1", "ResearchAgent"),
            _result("run-1"),
            _call("run-2", "CrawlStoriesAgent"),
            _result("run-2"),
        ]
        result = svc._merge_tool_activities(messages, activities, ["run-1", "run-2"])

        tool_names_last = [tc["tool"] for tc in result[3]["tool_calls"]]
        assert tool_names_last == ["CrawlStoriesAgent"]
        # The first assistant must not carry tools from run-2.
        assert "ResearchAgent" in [tc["tool"] for tc in result[1]["tool_calls"]]
        assert "CrawlStoriesAgent" not in [tc["tool"] for tc in result[1]["tool_calls"]]

    def test_turn_without_tools_leaves_assistant_untouched(self):
        """An intermediate turn with no tool calls keeps ordering intact
        via the right-anchor: the later turn's tools still land on the
        later assistant, not the middle one."""
        svc = SessionService()
        messages = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "A"},    # has tools
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "B"},    # no tools
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "C"},    # has tools
        ]
        activities = [
            _call("run-1", "ToolOne"),
            _result("run-1"),
            _call("run-3", "ToolThree"),
            _result("run-3"),
        ]
        # run-2 existed (turn without tools) so ordered_run_ids has 3 entries,
        # but activities only carry run-1 and run-3.
        ordered = ["run-1", "run-2", "run-3"]
        result = svc._merge_tool_activities(messages, activities, ordered)

        assert result[1].get("tool_calls", [{}])[0].get("tool") == "ToolOne"
        assert "tool_calls" not in result[3] or not result[3]["tool_calls"]
        assert result[5]["tool_calls"][0]["tool"] == "ToolThree"

    def test_ignores_orphan_activities_without_run_id(self):
        """Legacy rows (run_id=NULL) can't be placed reliably — ignored."""
        svc = SessionService()
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        orphan_call = _call(None, "LegacyTool")
        orphan_call["run_id"] = None
        orphan_result = _result(None)
        orphan_result["run_id"] = None
        activities = [orphan_call, orphan_result]
        result = svc._merge_tool_activities(messages, activities, [])
        assert "tool_calls" not in result[1]

    def test_empty_inputs_return_messages_unchanged(self):
        svc = SessionService()
        messages = [{"role": "assistant", "content": "x"}]
        assert svc._merge_tool_activities(messages, [], []) == messages

    def test_more_run_ids_than_assistants_drops_oldest(self):
        """A failed turn leaves a 'started' row with a run_id but no
        assistant message. The right-anchor keeps the newest run_ids on
        the existing assistants and drops the oldest orphan run_ids."""
        svc = SessionService()
        messages = [
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "B"},
        ]
        activities = [
            _call("run-failed", "OrphanTool"),
            _result("run-failed"),
            _call("run-live", "LiveTool"),
            _result("run-live"),
        ]
        result = svc._merge_tool_activities(
            messages, activities, ["run-failed", "run-live"]
        )
        assert [tc["tool"] for tc in result[1]["tool_calls"]] == ["LiveTool"]
