"""Unit tests for the agent_definitions DB store.

Real SQLite, no mocks — these tests exercise the cross-layer invariant
that JSON columns round-trip cleanly and the rev counter increments
exactly once per mutation. Mocking SQLite here would defeat the
purpose; the bug class we are guarding against is precisely
serialization / encoding drift.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ── Fixture: per-test DB pointed at by SPAWN_REGISTRY_DB ──────────────


@pytest.fixture()
def defs_db(tmp_path, monkeypatch):
    """Empty SQLite path bound via SPAWN_REGISTRY_DB; tables are created
    lazily by the service's first _connect()."""
    db_path = str(tmp_path / "test_agent_defs.db")
    monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)
    yield db_path


# ── Helpers ──────────────────────────────────────────────────────────


def _import():
    """Late import so monkeypatched env is visible."""
    from services import agent_definitions as svc
    return svc


# ── CRUD round-trip ──────────────────────────────────────────────────


def test_create_and_get_roundtrip(defs_db):
    svc = _import()
    created = svc.create_definition(
        name="ResearchBot",
        instruction="You research things.",
        servers=["serpapi", "scrapling-server"],
        tools={"serpapi": ["search"]},
        skills=[".fast-agent/skills/research"],
        model="anthropic.claude-sonnet",
        use_history=True,
        request_params={"parallel_tool_calls": True},
    )

    assert created["name"] == "ResearchBot"
    assert created["instruction"] == "You research things."
    assert created["servers"] == ["serpapi", "scrapling-server"]
    assert created["tools"] == {"serpapi": ["search"]}
    assert created["skills"] == [".fast-agent/skills/research"]
    assert created["model"] == "anthropic.claude-sonnet"
    assert created["use_history"] is True
    assert created["request_params"] == {"parallel_tool_calls": True}
    assert isinstance(created["created_at"], float)
    assert created["created_at"] == created["updated_at"]

    fetched = svc.get_definition("ResearchBot")
    assert fetched == created


def test_create_minimal_required_only(defs_db):
    """Only name + instruction are required. JSON cols default empty."""
    svc = _import()
    created = svc.create_definition(name="Minimal", instruction="hi")
    assert created["servers"] == []
    assert created["tools"] == {}
    assert created["skills"] == []
    assert created["request_params"] == {}
    assert created["model"] is None
    assert created["use_history"] is True  # default


def test_get_missing_returns_none(defs_db):
    svc = _import()
    assert svc.get_definition("NoSuchAgent") is None


def test_list_returns_all_ordered_by_created_at(defs_db):
    svc = _import()
    svc.create_definition(name="A", instruction="a")
    svc.create_definition(name="B", instruction="b")
    svc.create_definition(name="C", instruction="c")

    names = [d["name"] for d in svc.list_definitions()]
    assert names == ["A", "B", "C"]


# ── Update ───────────────────────────────────────────────────────────


def test_update_partial_keeps_other_fields(defs_db):
    svc = _import()
    svc.create_definition(
        name="Up",
        instruction="orig",
        servers=["s1"],
        skills=["sk1"],
    )

    updated = svc.update_definition("Up", instruction="new", servers=["s1", "s2"])
    assert updated["instruction"] == "new"
    assert updated["servers"] == ["s1", "s2"]
    # untouched fields preserved
    assert updated["skills"] == ["sk1"]
    assert updated["updated_at"] > updated["created_at"]


def test_update_unknown_field_raises(defs_db):
    svc = _import()
    svc.create_definition(name="X", instruction="x")
    with pytest.raises(ValueError, match="unknown fields"):
        svc.update_definition("X", nonsense=1)


def test_update_missing_agent_raises(defs_db):
    svc = _import()
    with pytest.raises(ValueError, match="not found"):
        svc.update_definition("Ghost", instruction="hi")


def test_update_no_fields_raises(defs_db):
    svc = _import()
    svc.create_definition(name="X", instruction="x")
    with pytest.raises(ValueError, match="no fields"):
        svc.update_definition("X")


def test_update_use_history_false_persists_as_zero(defs_db):
    svc = _import()
    svc.create_definition(name="U", instruction="u", use_history=True)
    updated = svc.update_definition("U", use_history=False)
    assert updated["use_history"] is False
    # confirm it survives a fresh get
    fetched = svc.get_definition("U")
    assert fetched["use_history"] is False


# ── Delete ───────────────────────────────────────────────────────────


def test_delete_returns_true_then_false(defs_db):
    svc = _import()
    svc.create_definition(name="D", instruction="d")
    assert svc.delete_definition("D") is True
    assert svc.get_definition("D") is None
    # idempotent — second delete returns False, no raise
    assert svc.delete_definition("D") is False


# ── Validation ───────────────────────────────────────────────────────


def test_create_duplicate_name_raises(defs_db):
    svc = _import()
    svc.create_definition(name="Dup", instruction="first")
    with pytest.raises(ValueError, match="already exists"):
        svc.create_definition(name="Dup", instruction="second")


def test_create_empty_name_raises(defs_db):
    svc = _import()
    with pytest.raises(ValueError, match="non-empty"):
        svc.create_definition(name="", instruction="x")


def test_create_empty_instruction_raises(defs_db):
    svc = _import()
    with pytest.raises(ValueError, match="instruction"):
        svc.create_definition(name="X", instruction="   ")


# ── Rev counter ──────────────────────────────────────────────────────


def test_rev_starts_at_zero(defs_db):
    svc = _import()
    assert svc.get_rev() == 0


def test_rev_increments_on_each_mutation(defs_db):
    svc = _import()
    assert svc.get_rev() == 0

    svc.create_definition(name="A", instruction="a")
    assert svc.get_rev() == 1

    svc.update_definition("A", instruction="b")
    assert svc.get_rev() == 2

    svc.delete_definition("A")
    assert svc.get_rev() == 3


def test_rev_not_bumped_on_failed_create(defs_db):
    """Validation failure → no rev bump. Otherwise the reload poller
    would spin uselessly."""
    svc = _import()
    svc.create_definition(name="X", instruction="x")
    rev_before = svc.get_rev()

    with pytest.raises(ValueError):
        svc.create_definition(name="X", instruction="second")  # duplicate
    assert svc.get_rev() == rev_before


def test_rev_not_bumped_on_delete_of_missing(defs_db):
    svc = _import()
    rev_before = svc.get_rev()
    assert svc.delete_definition("NoSuch") is False
    assert svc.get_rev() == rev_before


# ── JSON column edge cases ────────────────────────────────────────────


def test_json_columns_survive_unicode(defs_db):
    """Unicode in instruction + JSON values must roundtrip without
    re-encoding (ensure_ascii=False is the contract)."""
    svc = _import()
    svc.create_definition(
        name="UnicodeBot",
        instruction="Speak naturally — emojis OK 🌐",
        servers=["máy-chủ-1"],  # contrived but legal SQLite
    )
    fetched = svc.get_definition("UnicodeBot")
    assert "🌐" in fetched["instruction"]
    assert fetched["servers"] == ["máy-chủ-1"]


def test_tools_filter_map_with_nested_lists(defs_db):
    svc = _import()
    svc.create_definition(
        name="T",
        instruction="t",
        tools={"srv-a": ["tool1", "tool2"], "srv-b": ["tool3"]},
    )
    fetched = svc.get_definition("T")
    assert fetched["tools"] == {"srv-a": ["tool1", "tool2"], "srv-b": ["tool3"]}


# ── Schema: confirm table exists & meta seeded ───────────────────────


def test_tables_created_on_first_use(defs_db):
    svc = _import()
    # Trigger _ensure_tables via a read.
    svc.get_rev()

    conn = sqlite3.connect(defs_db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert {"agent_definitions", "agent_definitions_meta"} <= tables

        meta = conn.execute(
            "SELECT value FROM agent_definitions_meta WHERE key = 'rev'"
        ).fetchone()
        assert meta == ("0",)
    finally:
        conn.close()
