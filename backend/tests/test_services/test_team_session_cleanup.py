"""Regression tests for team-session cleanup.

Single source of truth for the ``team_sessions`` table is
``fast_agent.spawn.registry_backends.TeamSessionStore``. The
``team_spawner`` module owns the public CRUD API on top of it. These
tests pin:

* ``delete_team_session`` removes the row from SQLite. (Originally
  this test also asserted "clears in-memory cache" — that cache was
  removed on 2026-05-14 because it silently went stale whenever a
  sibling subprocess upserted. With the store as sole SoT, every
  read goes to SQLite, so there's nothing to evict.)
* ``delete_team_sessions_by_team_name`` matches by stored ``team_name``
  and deletes every match from the store.
* The Jarvis-side ``AgentRegistryDB`` is kept as **read-only** view —
  removed methods stay removed (no parallel write path that could
  diverge from the SoT).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ─── Fixture: isolate team_spawner globals + stub TeamSessionStore ────


@pytest.fixture
def isolated_spawner(monkeypatch):
    """Reset team_spawner singletons + swap the SQLite store with a mock."""
    import fast_agent.spawn.team_spawner as spawner

    # Snapshot then clear the store reference (the cache that used to
    # live alongside it was removed — no need to save/restore).
    saved_store = spawner._team_store
    spawner._team_store = None

    fake_store = MagicMock()
    fake_store.get = MagicMock(return_value=None)
    fake_store.list_all = MagicMock(return_value=[])
    fake_store.delete = MagicMock()

    monkeypatch.setattr(spawner, "_get_store", lambda: fake_store)

    yield spawner, fake_store

    # Restore.
    spawner._team_store = saved_store


# ─── delete_team_session ──────────────────────────────────────────


def test_delete_team_session_drops_row(isolated_spawner):
    spawner, fake_store = isolated_spawner
    fake_store.get.return_value = {"session_id": "abc"}

    deleted = spawner.delete_team_session("abc")

    assert deleted is True, "Returns True when row existed in store"
    fake_store.delete.assert_called_once_with("abc")


def test_delete_team_session_returns_false_when_unknown(isolated_spawner):
    spawner, fake_store = isolated_spawner
    fake_store.get.return_value = None  # no such row

    deleted = spawner.delete_team_session("ghost")

    assert deleted is False
    # Still call store.delete (it's idempotent — DELETE WHERE no-match)
    # but contract is the boolean return reflecting prior existence.
    fake_store.delete.assert_called_once_with("ghost")


# ─── delete_team_sessions_by_team_name ────────────────────────────


def test_delete_team_sessions_by_team_name_filters_by_team(isolated_spawner):
    spawner, fake_store = isolated_spawner
    fake_store.list_all.return_value = [
        {"session_id": "a1", "team_name": "alpha"},
        {"session_id": "a2", "team_name": "alpha"},
        {"session_id": "b1", "team_name": "beta"},
    ]

    count = spawner.delete_team_sessions_by_team_name("alpha")

    assert count == 2
    assert fake_store.delete.call_count == 2
    deleted_ids = {c.args[0] for c in fake_store.delete.call_args_list}
    assert deleted_ids == {"a1", "a2"}


def test_delete_team_sessions_by_team_name_unknown_team_is_noop(isolated_spawner):
    spawner, fake_store = isolated_spawner
    fake_store.list_all.return_value = [
        {"session_id": "a1", "team_name": "alpha"},
    ]

    count = spawner.delete_team_sessions_by_team_name("ghost")

    assert count == 0
    fake_store.delete.assert_not_called()


def test_delete_team_sessions_by_team_name_skips_rows_missing_session_id(isolated_spawner):
    """Defensive: a malformed row without ``session_id`` must not crash
    the sweep (and must not spuriously bump the deleted counter).
    """
    spawner, fake_store = isolated_spawner
    fake_store.list_all.return_value = [
        {"session_id": "a1", "team_name": "alpha"},
        {"team_name": "alpha"},  # ← no session_id
        {"session_id": "a2", "team_name": "alpha"},
    ]

    count = spawner.delete_team_sessions_by_team_name("alpha")

    assert count == 2  # only the two with valid session_id
    deleted_ids = {c.args[0] for c in fake_store.delete.call_args_list}
    assert deleted_ids == {"a1", "a2"}


# ─── SoT discipline: AgentRegistryDB does NOT own writes ──────────


def test_agent_registry_db_has_no_team_session_writers():
    """AgentRegistryDB is a read-only viewer onto ``team_sessions``.

    Earlier in this debugging session a duplicate ``delete_team_session``
    was added directly to ``AgentRegistryDB`` — that violated single
    source of truth. Pin the contract: no writers on the registry side.
    """
    from core.agent_registry_db import AgentRegistryDB

    forbidden = {
        "delete_team_session",
        "delete_team_sessions_by_team_name",
        "upsert_team_session",
        "save_team_session",
    }
    present = forbidden & set(dir(AgentRegistryDB))
    assert not present, (
        f"AgentRegistryDB must NOT have team_session writers — go through "
        f"fast_agent.spawn.team_spawner instead. Found: {present}"
    )
