"""The isolated team runner must install Jarvis's model hook before its first call."""
from types import SimpleNamespace
import json
import sqlite3

from fast_agent.spawn.isolated_runner import _install_tool_hooks


def test_isolated_runner_installs_model_hook(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE spawn_registry (run_id TEXT PRIMARY KEY, data_json TEXT NOT NULL)")
        conn.execute("INSERT INTO spawn_registry VALUES (?, ?)", (
            "run-1", json.dumps({"agent_name": "Member", "session_id": "team-1"}),
        ))
    child = SimpleNamespace(tool_runner_hooks=None, name="Member")

    class App(dict):
        @property
        def _agents(self):
            return self

    app = App(Member=child)
    _install_tool_hooks(app, "run-1", "Member")
    assert child.tool_runner_hooks is not None
    assert getattr(child, "_jarvis_model_hook", False)


def test_isolated_runner_without_jarvis_registry_keeps_generic_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(tmp_path / "empty.db"))
    child = SimpleNamespace(tool_runner_hooks=None, name="Member")

    class App(dict):
        @property
        def _agents(self):
            return self

    _install_tool_hooks(App(Member=child), "run-1", "Member")
    assert child.tool_runner_hooks is not None
    assert not getattr(child, "_jarvis_model_hook", False)
