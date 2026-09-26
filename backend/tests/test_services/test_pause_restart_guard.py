"""A backend restart must not reactivate a manually paused team."""

import json
import sqlite3
from unittest.mock import patch

from core.agent_registry_db import AgentRegistryDB
from fast_agent.spawn.servers._team_helpers import auto_wake_if_idle


def test_stale_registry_keeps_dead_team_member_paused(monkeypatch, tmp_path):
    db_path = tmp_path / "registry.db"
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE spawn_registry (run_id TEXT PRIMARY KEY, data_json TEXT NOT NULL)")
        for run_id, status in (("paused-run", "paused"), ("running-run", "running")):
            conn.execute(
                "INSERT INTO spawn_registry VALUES (?, ?)",
                (run_id, json.dumps({
                    "agent_name": run_id, "status": status,
                    "lifecycle": "resumable", "pid": 99999999,
                })),
            )
    registry = AgentRegistryDB()
    assert registry.mark_stale_running() == 2
    records = registry.get_all()
    assert records["paused-run"]["status"] == "paused"
    assert records["paused-run"]["pid"] is None
    assert records["running-run"]["status"] == "idle"


def test_durable_pause_blocks_auto_wake_before_liveness_probe(monkeypatch, tmp_path):
    db_path = tmp_path / "registry.db"
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE agent_pause_state (agent_name TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO agent_pause_state VALUES ('Taylor [Dev]')")
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive") as probe:
        auto_wake_if_idle("Taylor [Dev]")
    probe.assert_not_called()


def test_resume_row_removal_allows_auto_wake(monkeypatch, tmp_path):
    db_path = tmp_path / "registry.db"
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE agent_pause_state (agent_name TEXT PRIMARY KEY)")
    with patch("fast_agent.spawn.agent_channel.AgentChannel.is_alive", return_value=True) as probe, patch(
        "fast_agent.spawn.servers._team_helpers._wake_alive_agent"
    ) as wake:
        auto_wake_if_idle("Taylor [Dev]")
    probe.assert_called_once_with("Taylor [Dev]")
    wake.assert_called_once_with("Taylor [Dev]")
