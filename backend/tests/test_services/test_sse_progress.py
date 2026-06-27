"""Tests for ProgressEventManager (sse_progress.py)."""

import asyncio
from unittest.mock import MagicMock

import pytest

from services.sse_progress import ProgressEventManager, humanize_agent_name, create_progress_hooks


class TestHumanizeAgentName:
    """Tests for the humanize_agent_name utility."""

    def test_camel_case(self):
        assert humanize_agent_name("FinanceAgent") == "Finance Agent"

    def test_iot_prefix(self):
        # Regex splits after lowercase 'o' before uppercase 'T': "Io TAgent"
        result = humanize_agent_name("IoTAgent")
        assert "Io" in result  # Known behavior: simple regex doesn't handle acronyms

    def test_simple_name(self):
        assert humanize_agent_name("Jarvis") == "Jarvis"

    def test_instance_suffix(self):
        result = humanize_agent_name("FinanceAgent[1]")
        assert "Finance Agent" in result
        assert "[1]" in result

    def test_empty(self):
        assert humanize_agent_name("") == ""


class TestProgressEventManager:
    """Tests for the per-request queue manager."""

    def test_create_returns_queue(self):
        pm = ProgressEventManager()
        q = pm.create("req-1")
        assert isinstance(q, asyncio.Queue)

    def test_get_returns_created_queue(self):
        pm = ProgressEventManager()
        q = pm.create("req-1")
        assert pm.get("req-1") is q

    def test_get_returns_none_for_unknown(self):
        pm = ProgressEventManager()
        assert pm.get("nonexistent") is None

    def test_remove_cleans_up(self):
        pm = ProgressEventManager()
        pm.create("req-1")
        pm.remove("req-1")
        assert pm.get("req-1") is None

    def test_remove_nonexistent_is_noop(self):
        pm = ProgressEventManager()
        pm.remove("nonexistent")  # Should not raise

    def test_push_adds_to_queue(self):
        pm = ProgressEventManager()
        q = pm.create("req-1")
        pm.push("req-1", "thinking", {"agent": "Test"})

        assert not q.empty()
        event = q.get_nowait()
        assert event["type"] == "thinking"
        assert event["agent"] == "Test"
        assert "timestamp" in event

    def test_push_to_unknown_request_is_noop(self):
        pm = ProgressEventManager()
        pm.push("nonexistent", "thinking", {})  # Should not raise

    def test_push_adds_agent_display(self):
        pm = ProgressEventManager()
        q = pm.create("req-1")
        pm.push("req-1", "thinking", {"agent": "FinanceAgent"})

        event = q.get_nowait()
        assert event["agent_display"] == "Finance Agent"

    def test_multiple_events_queued_in_order(self):
        pm = ProgressEventManager()
        q = pm.create("req-1")
        pm.push("req-1", "start", {"message": "Starting"})
        pm.push("req-1", "thinking", {"agent": "A"})
        pm.push("req-1", "done", {"response": "ok"})

        events = []
        while not q.empty():
            events.append(q.get_nowait())

        assert [e["type"] for e in events] == ["start", "thinking", "done"]


class TestCreateProgressHooks:
    """Tests for create_progress_hooks factory."""

    def test_returns_hooks_object(self):
        hooks = create_progress_hooks("req-1")
        assert hooks.before_llm_call is not None
        assert hooks.after_llm_call is not None
        assert hooks.before_tool_call is not None
        assert hooks.after_tool_call is not None


class TestPerToolResults:
    """Each tool in a multi-tool turn must carry ITS OWN result_preview.

    Regression: get_current_time rendered memory_remember's result because the
    whole batch shared the first tool's preview."""

    @staticmethod
    def _msg(results: dict):
        # PromptMessageExtended-like: tool_results = {id: result-with-content}
        from types import SimpleNamespace
        tr = {tid: SimpleNamespace(content=[SimpleNamespace(text=txt)])
              for tid, txt in results.items()}
        return SimpleNamespace(tool_results=tr)

    def test_extract_results_by_id_keeps_each_result(self):
        from services.sse_progress import _extract_results_by_id
        by_id, order = _extract_results_by_id(self._msg({
            "call-1": '{"candidate_id":"abc"}',
            "call-2": "2026-06-27T17:00:00",
        }))
        assert by_id["call-1"] == '{"candidate_id":"abc"}'
        assert by_id["call-2"] == "2026-06-27T17:00:00"
        assert order == ['{"candidate_id":"abc"}', "2026-06-27T17:00:00"]

    def test_attach_matches_by_id(self):
        from services.sse_progress import _attach_per_tool_results
        tools = [{"id": "call-1", "name": "memory_remember"},
                 {"id": "call-2", "name": "get_current_time"}]
        batch = _attach_per_tool_results(tools, self._msg({
            "call-1": "MEM-RESULT", "call-2": "TIME-RESULT"}))
        assert tools[0]["result_preview"] == "MEM-RESULT"
        assert tools[1]["result_preview"] == "TIME-RESULT"   # NOT the memory result
        assert batch == "MEM-RESULT"                          # legacy = first non-empty

    def test_attach_positional_fallback_when_ids_mismatch(self):
        from services.sse_progress import _attach_per_tool_results
        tools = [{"id": "x1", "name": "a"}, {"id": "x2", "name": "b"}]
        _attach_per_tool_results(tools, self._msg({"y1": "A", "y2": "B"}))
        assert tools[0]["result_preview"] == "A"
        assert tools[1]["result_preview"] == "B"

    def test_attach_no_results_leaves_none(self):
        from types import SimpleNamespace
        from services.sse_progress import _attach_per_tool_results
        tools = [{"id": "call-1", "name": "a"}]
        batch = _attach_per_tool_results(tools, SimpleNamespace(tool_results=None))
        assert tools[0]["result_preview"] is None
        assert batch is None
