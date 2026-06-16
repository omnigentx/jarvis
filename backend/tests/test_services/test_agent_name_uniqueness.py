"""Uniqueness gate for agent creation (workstream 01b).

``ensure_unique_agent_name`` is the single shared check called by the three
creation entry points. It unions live registry + team sessions + persistent
``agent_definitions`` and rejects collisions. Resume paths never call it.
"""

import sqlite3
from dataclasses import dataclass

import pytest

from fast_agent.spawn import team_spawner
from fast_agent.spawn.team_spawner import (
    _collect_taken_names,
    ensure_unique_agent_name,
)


@dataclass
class _Rec:
    agent_name: str


class _FakeRegistry:
    def __init__(self, names):
        self._names = names

    def list_active(self):
        return [_Rec(n) for n in self._names]


@pytest.fixture(autouse=True)
def _isolate_team_store(monkeypatch):
    """Stop ``_collect_taken_names`` from reading the real on-disk team store
    so tests only see names we inject. Opt-in per the real source under test
    (registry + db_path) stays live."""

    class _EmptyStore:
        def list_all(self):
            return []

    monkeypatch.setattr(team_spawner, "_get_store", lambda: _EmptyStore())
    # Ensure no ambient SPAWN_REGISTRY_DB leaks in.
    monkeypatch.delenv("SPAWN_REGISTRY_DB", raising=False)


def _make_definitions_db(path, names):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE agent_definitions (name TEXT PRIMARY KEY, created_at REAL)"
    )
    conn.executemany(
        "INSERT INTO agent_definitions (name, created_at) VALUES (?, 0)",
        [(n,) for n in names],
    )
    conn.commit()
    conn.close()


def test_collect_unions_registry_and_definitions(tmp_path):
    db = tmp_path / "defs.db"
    _make_definitions_db(db, ["researcher", "planner"])
    registry = _FakeRegistry(["Riley [SA]"])

    taken = _collect_taken_names(registry, str(db))

    assert {"Riley [SA]", "researcher", "planner"} <= taken


def test_collect_tolerates_missing_table(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()  # exists but no agent_definitions table
    registry = _FakeRegistry(["Jarvis"])

    # Missing supplementary table must not raise; live registry still counts.
    taken = _collect_taken_names(registry, str(db))
    assert taken == {"Jarvis"}


def test_ensure_raises_on_registry_collision():
    registry = _FakeRegistry(["Jarvis"])
    with pytest.raises(ValueError, match="already exists"):
        ensure_unique_agent_name("Jarvis", registry=registry)


def test_ensure_raises_on_definitions_collision(tmp_path):
    db = tmp_path / "defs.db"
    _make_definitions_db(db, ["researcher"])
    registry = _FakeRegistry([])
    with pytest.raises(ValueError, match="already exists"):
        ensure_unique_agent_name("researcher", registry=registry, db_path=str(db))


def test_ensure_passes_for_fresh_name(tmp_path):
    db = tmp_path / "defs.db"
    _make_definitions_db(db, ["researcher"])
    registry = _FakeRegistry(["Jarvis"])
    # No raise → unique.
    ensure_unique_agent_name("new_agent", registry=registry, db_path=str(db))


def test_ensure_rejects_empty_name():
    registry = _FakeRegistry([])
    with pytest.raises(ValueError, match="non-empty"):
        ensure_unique_agent_name("   ", registry=registry)


def test_generate_unique_avoids_taken(tmp_path, monkeypatch):
    # Generated team names must dodge collisions reported by _collect_taken_names.
    registry = _FakeRegistry([])
    name = team_spawner._generate_unique_agent_name("QE", registry)
    assert name.endswith("[QE]")
