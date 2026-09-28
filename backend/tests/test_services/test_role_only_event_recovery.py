"""A subprocess role-only event must not replace a team member's identity."""

import json
import sqlite3
from unittest.mock import MagicMock

from core.agent_registry_db import AgentRegistryDB
from services.spawn_progress_bridge import SpawnProgressBridge


def test_role_only_error_keeps_registered_name_and_startup_repairs_legacy_row(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "registry.db"
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE spawn_registry (run_id TEXT PRIMARY KEY, data_json TEXT NOT NULL)")

    registry = AgentRegistryDB()
    registry.upsert_record("run-bailey", {
        "agent_name": "Bailey [PM]", "name": "Bailey [PM]",
        "original_config": {"agent_name": "Bailey [PM]"},
        "role": "pm", "team_name": "team-b", "session_id": "session-b",
        "status": "running", "lifecycle": "resumable",
    })
    bridge = SpawnProgressBridge(MagicMock())
    bridge._registry_db = registry
    bridge._persist_activity = MagicMock()
    bridge._broadcast_activity = MagicMock()
    bridge._on_member_state_event = MagicMock()

    bridge._process_event_line(json.dumps({
        "run_id": "run-bailey", "agent_name": "pm", "event_type": "error",
        "data": {"message": "connection lost"},
    }))

    assert registry.get_record("run-bailey")["agent_name"] == "Bailey [PM]"
    assert registry.find_by_name("pm") == []
    bridge._broadcast_activity.assert_called_once()
    assert bridge._broadcast_activity.call_args.args[0] == "Bailey [PM]"

    # A row written by an older backend is repaired regardless of status.
    registry.upsert_record("run-bailey", {"agent_name": "pm", "name": "pm"})
    assert registry.mark_stale_running() == 1
    assert registry.get_record("run-bailey")["agent_name"] == "Bailey [PM]"
    assert registry.find_by_name("pm") == []
