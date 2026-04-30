"""Tests for SpawnProgressBridge — event processing and SSE routing."""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from services.spawn_progress_bridge import SpawnProgressBridge



class TestSpawnProgressBridge:
    """Tests for SpawnProgressBridge event mapping and routing."""

    def _make_bridge(self) -> tuple[SpawnProgressBridge, MagicMock]:
        pm = MagicMock()
        bridge = SpawnProgressBridge(pm)
        return bridge, pm

    # ─── Event mapping tests (via _process_event_line) ───

    def test_thinking_event_is_filtered(self):
        """thinking events are intentionally filtered from chat SSE (no useful content)."""
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-123")

        line = json.dumps({"agent_name": "Linh - PM", "event_type": "thinking", "data": {"model": "gpt-4o"}})
        bridge._process_event_line(line)

        pm.push.assert_not_called()

    def test_tool_call_event_includes_tools_list(self):
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-123")

        line = json.dumps({
            "agent_name": "Dev", "event_type": "tool_call",
            "data": {"tool_name": "write_file", "args_preview": "/src/main.py"},
        })
        bridge._process_event_line(line)

        data = pm.push.call_args[0][2]
        assert data["tools"][0]["name"] == "write_file"
        assert data["tools"][0]["args"]["preview"] == "/src/main.py"


    def test_email_tool_call_redacts_body_from_sse_payload(self):
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-123")

        line = json.dumps({
            "role": "Dev", "event_type": "tool_call",
            "data": {
                "tool_name": "email__send_email",
                "args_preview": '{"to":"Hoa - BA","subject":"Hi","body":"secret roadmap contents"}',
            },
        })
        bridge._process_event_line(line)

        data = pm.push.call_args[0][2]
        preview = data["tools"][0]["args"]["preview"]
        assert "secret roadmap contents" not in preview
        assert "body" not in preview.lower()

    def test_email_tool_call_redacts_body_from_message(self):
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-123")

        line = json.dumps({
            "role": "Dev", "event_type": "tool_call",
            "data": {
                "tool_name": "send_email",
                "args_preview": 'to=all body=very sensitive incident details subject=FYI',
            },
        })
        bridge._process_event_line(line)

        data = pm.push.call_args[0][2]
        assert "very sensitive incident details" not in data["message"]
        assert "very sensitive incident details" not in data["tools"][0]["args"]["preview"]

    def test_tool_result_event_includes_duration(self):
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-123")

        line = json.dumps({
            "agent_name": "Dev", "event_type": "tool_result",
            "data": {"tool_name": "write_file", "status": "ok", "duration_ms": 1500.5},
        })
        bridge._process_event_line(line)

        data = pm.push.call_args[0][2]
        assert data["duration_ms"] == 1500

    def test_result_event_maps_to_spawn_result(self):
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-123")

        line = json.dumps({
            "agent_name": "Hoa - BA", "event_type": "result",
            "data": {"summary": "Wrote BRD", "duration_seconds": 45.3},
        })
        bridge._process_event_line(line)

        args = pm.push.call_args
        assert args[0][1] == "spawn_result"
        assert "45s" in args[0][2]["message"]

    def test_error_event_maps_to_spawn_error(self):
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-123")

        line = json.dumps({
            "agent_name": "QE", "event_type": "error",
            "data": {"message": "Connection timeout to filesystem server"},
        })
        bridge._process_event_line(line)

        args = pm.push.call_args
        assert args[0][1] == "spawn_error"
        assert "Connection timeout" in args[0][2]["message"]

    def test_started_event(self):
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-123")

        line = json.dumps({
            "agent_name": "Dev", "event_type": "started",
            "data": {"model": "gpt-4o", "servers": ["filesystem"]},
        })
        bridge._process_event_line(line)

        assert pm.push.call_args[0][1] == "spawn_started"

    def test_mcp_connected_event(self):
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-123")

        line = json.dumps({
            "agent_name": "Dev", "event_type": "mcp_connected",
            "data": {"server_name": "filesystem", "status": "ok"},
        })
        bridge._process_event_line(line)

        assert pm.push.call_args[0][1] == "spawn_mcp"
        assert "✓" in pm.push.call_args[0][2]["message"]

    def test_unknown_event_maps_to_spawn_info(self):
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-123")

        line = json.dumps({"agent_name": "Dev", "event_type": "custom_event", "data": {}})
        bridge._process_event_line(line)

        assert pm.push.call_args[0][1] == "spawn_info"

    # ─── Request ID routing tests ───

    def test_no_push_when_no_request_id(self):
        bridge, pm = self._make_bridge()
        # No set_request_id called

        line = json.dumps({"agent_name": "Dev", "event_type": "thinking", "data": {}})
        bridge._process_event_line(line)

        pm.push.assert_not_called()

    def test_push_stops_after_clearing_request_id(self):
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-123")

        line1 = json.dumps({"agent_name": "Dev", "event_type": "started", "data": {"model": "gpt-4o", "servers": []}})
        bridge._process_event_line(line1)
        assert pm.push.call_count == 1

        bridge.set_request_id(None)

        line2 = json.dumps({"agent_name": "Dev", "event_type": "result", "data": {"duration_seconds": 10}})
        bridge._process_event_line(line2)
        assert pm.push.call_count == 1  # no additional push

    def test_request_id_can_be_changed(self):
        bridge, pm = self._make_bridge()

        bridge.set_request_id("req-A")
        bridge._process_event_line(json.dumps({"agent_name": "Dev", "event_type": "started", "data": {"model": "gpt-4o", "servers": []}}))
        assert pm.push.call_args[0][0] == "req-A"

        bridge.set_request_id("req-B")
        bridge._process_event_line(json.dumps({"agent_name": "BA", "event_type": "started", "data": {"model": "gpt-4o", "servers": []}}))
        assert pm.push.call_args[0][0] == "req-B"

    # ─── Agent name passthrough ───

    def test_agent_and_agent_display_set_to_role(self):
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-123")

        line = json.dumps({"agent_name": "Linh - PM", "event_type": "started", "data": {"model": "gpt-4o", "servers": []}})
        bridge._process_event_line(line)

        data = pm.push.call_args[0][2]
        assert data["agent"] == "Linh - PM"
        assert data["agent_display"] == "Linh - PM"

    # ─── Error message truncation ───

    def test_error_message_truncated_to_100_chars(self):
        bridge, pm = self._make_bridge()
        bridge.set_request_id("req-123")

        long_msg = "x" * 200
        line = json.dumps({"agent_name": "Dev", "event_type": "error", "data": {"message": long_msg}})
        bridge._process_event_line(line)

        data = pm.push.call_args[0][2]
        assert len(data["message"]) < 200

    # ─── File operations tests ───





    def test_broadcast_activity_redacts_email_body_from_data(self):
        bridge, pm = self._make_bridge()

        from unittest.mock import patch
        with patch("services.activity_stream.activity_stream_manager.broadcast") as broadcast:
            bridge._broadcast_activity(
                "Dev",
                "tool_call",
                {
                    "tool_name": "send_email",
                    "args_preview": '{"to":"all","subject":"FYI","body":"super secret body text"}',
                },
                {"run_id": "run-1", "timestamp": 123},
            )

        payload = broadcast.call_args[0][0]
        assert "super secret body text" not in json.dumps(payload, ensure_ascii=False)
        assert "body" not in json.dumps(payload["data"], ensure_ascii=False).lower()

    def test_persist_activity_redacts_email_body_from_data_json(self):
        bridge, pm = self._make_bridge()

        fake_db = MagicMock()

        class FakeActivity:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        from unittest.mock import patch
        with patch("core.database.get_db_session", return_value=fake_db), patch("core.database.AgentActivity", FakeActivity):
            bridge._persist_activity(
                "Dev",
                "tool_call",
                {
                    "tool_name": "email__send_email",
                    "args_preview": '{"to":"Hoa - BA","subject":"Hi","body":"super secret body text"}',
                },
                {"run_id": "run-1", "timestamp": 123},
            )

        activity = fake_db.add.call_args[0][0]
        assert "super secret body text" not in activity.kwargs["data_json"]
        assert "body" not in activity.kwargs["data_json"].lower()

    def test_registry_upsert_handles_lifecycle_spawn_registered_event(self):
        registry_db = MagicMock()
        pm = MagicMock()
        bridge = SpawnProgressBridge(pm, registry_db=registry_db)

        bridge._upsert_spawn_record(
            "Dev",
            "lifecycle_spawn_registered",
            {"status": "starting"},
            {"run_id": "run-123", "timestamp": 1000},
        )

        registry_db.upsert_record.assert_called_once()
        run_id, record = registry_db.upsert_record.call_args[0]
        assert run_id == "run-123"
        assert record["status"] == "starting"
        assert record["agent_name"] == "Dev"


