"""Regression tests for ``services.pause_controller.PauseController``.

Guards the resume-doesn't-update-DB-status bug observed during the
2026-05-10 dev session: ``_broadcast_state_change`` looked up the
agent via ``registry_db.list_running()``, which filters to
``status IN ('running', 'pending')``. The resume path runs while the
agent is still ``paused`` in the DB, so the lookup returned no rows
→ ``upsert_record`` was skipped → DB status stayed ``"paused"``
forever. SSE consumers / UI badges then showed the agent as paused
even after in-memory state had flipped back to running.

Fix: use ``find_by_name`` (no status filter), mirroring the pattern
already in ``_find_pid``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fresh_manager():
    """Module-level singleton ``pause_controller`` survives across tests;
    we need a clean instance for deterministic assertions.
    """
    from services.pause_controller import PauseController
    return PauseController()


@pytest.fixture
def fake_registry(monkeypatch):
    """Stub ``services.shared_state.registry_db`` with a recording mock."""
    import services.shared_state as state

    original = state.registry_db
    fake = MagicMock()
    fake.upsert_record = MagicMock()
    state.registry_db = fake

    yield fake

    state.registry_db = original


def test_resume_updates_db_status_to_running(fresh_manager, fake_registry):
    """Resume MUST upsert status='running' on the agent's spawn_registry row.

    Pre-fix this failed because ``list_running()`` skipped the paused row.
    Post-fix the manager uses ``find_by_name`` which returns paused rows too.
    """
    # Registry has a paused row for the agent (the realistic state at the
    # moment resume() is called).
    fake_registry.find_by_name.return_value = [{
        "run_id": "run-123",
        "agent_name": "PM",
        "status": "paused",
    }]
    fake_registry.list_running.return_value = []  # paused row excluded — that was the bug

    # Manager must be in "paused" state before resume can flip it.
    fresh_manager.pause("PM")
    fake_registry.upsert_record.reset_mock()

    fresh_manager.resume("PM")

    fake_registry.upsert_record.assert_called_once_with(
        "run-123", {"status": "running"}
    )


def test_pause_updates_db_status_to_paused(fresh_manager, fake_registry):
    """Pause path: agent's row is found regardless of which lookup is used —
    keep the contract intact while we're at it.
    """
    fake_registry.find_by_name.return_value = [{
        "run_id": "run-456",
        "agent_name": "Dev",
        "status": "running",
    }]

    fresh_manager.pause("Dev")

    fake_registry.upsert_record.assert_called_once_with(
        "run-456", {"status": "paused"}
    )


def test_resume_handles_in_process_agent_with_no_registry_row(
    fresh_manager, fake_registry,
):
    """In-process agents (Jarvis) have no spawn_registry row.

    Resume must NOT crash and MUST still flip in-memory pause state.
    """
    fake_registry.find_by_name.return_value = []  # no row — Jarvis is in-process

    fresh_manager.pause("Jarvis")
    assert fresh_manager.is_paused("Jarvis") is True

    result = fresh_manager.resume("Jarvis")
    assert result is True
    assert fresh_manager.is_paused("Jarvis") is False
    # No upsert because no row to update — but no exception either.
    fake_registry.upsert_record.assert_not_called()


def test_resume_uses_find_by_name_not_list_running(fresh_manager, fake_registry):
    """Pin the lookup contract: must call ``find_by_name``, must NOT call
    ``list_running`` (the original buggy implementation).

    If a future refactor reverts to ``list_running``, this test fails loudly
    pointing back at the 2026-05-10 incident.
    """
    fake_registry.find_by_name.return_value = [{
        "run_id": "run-789",
        "agent_name": "QE",
        "status": "paused",
    }]

    fresh_manager.pause("QE")
    fake_registry.find_by_name.reset_mock()
    fake_registry.list_running.reset_mock()

    fresh_manager.resume("QE")

    fake_registry.find_by_name.assert_called_with("QE")
    fake_registry.list_running.assert_not_called()
