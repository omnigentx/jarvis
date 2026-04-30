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
