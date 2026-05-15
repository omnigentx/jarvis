"""Regression tests for the 2026-05-15 ``5612e8f3`` retro-meeting deadlock.

Production incident: PM (Bailey [PM]) was inject-resumed to run a sprint
retro. PM created a 7-participant meeting, did the kickoff speak, and
advanced the turn to Adrian [BA]. The meeting then deadlocked for
~20 minutes waiting for Adrian — who had been ``completed`` 48 minutes
earlier and never got respawned.

Root cause (path-asymmetry between writer and reader):
  - WRITER (``_team_helpers.get_bus``) reads ``TEAM_MESSAGES_DIR`` env
    var → session-scoped path like ``.runtime/state/messages/{sid}/``.
  - READER (``isolated_spawner._check_and_resume_on_inbox``) ignored
    ``TEAM_MESSAGES_DIR`` and only walked up ``TEAM_WORKSPACE`` to find
    ``.runtime/state/messages`` — WITHOUT the session segment.
  - Inbox messages were written to the session folder but read from the
    parent folder → ``bus.read_unread`` returned empty → silent return →
    no respawn → meeting stuck on Adrian's turn forever.

These tests pin two contracts that, taken together, would have
prevented the incident:

  1. ``_check_and_resume_on_inbox`` MUST read ``TEAM_MESSAGES_DIR``
     first (matching the writer side) before falling back to the
     workspace walk-up.
  2. When the function exits without spawning, it MUST log a warning
     describing *why* — silent returns are how we lost 20 minutes
     diagnosing this incident from scratch.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _restore_fast_agent_log_propagation():
    """``fast_agent.context.configure_logger`` sets ``propagate=False`` on
    the ``fast_agent`` logger when an agent's context boots (e.g. anywhere
    a prior e2e test called ``agent.generate(...)``). That breaks pytest's
    ``caplog`` for ALL descendant loggers (``fast_agent.spawn.*``) — log
    records emitted by the code under test never reach the root handler
    that caplog hooks, so the assertion that we logged a warning falsely
    fails.

    Each test in this file probes a fail-loud contract on a fast_agent
    descendant logger, so we MUST guarantee the propagation chain is
    intact regardless of leaked state from prior tests.
    """
    fa_logger = logging.getLogger("fast_agent")
    prev = fa_logger.propagate
    fa_logger.propagate = True
    try:
        yield
    finally:
        fa_logger.propagate = prev


@pytest.mark.asyncio
async def test_check_and_resume_reads_session_scoped_messages_dir(tmp_path, caplog):
    """When ``TEAM_MESSAGES_DIR`` env_vars points to a session-scoped folder
    that has unread inbox messages, the function MUST find them and
    proceed to spawn (instead of returning silently from the wrong path).
    """
    from fast_agent.spawn.message_bus import MessageBus
    from fast_agent.spawn.isolated_spawner import _check_and_resume_on_inbox

    # Lay out the EXACT structure used in production: session-scoped
    # subfolder under .runtime/state/messages/.
    backend_root = tmp_path / "backend"
    session_id = "deadbeef"
    workspace = backend_root / ".runtime" / "data" / "workspaces" / f"agile-team_{session_id}"
    workspace.mkdir(parents=True)
    session_msg_dir = backend_root / ".runtime" / "state" / "messages" / session_id
    session_msg_dir.mkdir(parents=True)

    # Writer side: drop an "unread" message into the session-scoped
    # inbox via the same MessageBus the production code uses. This is
    # how ``_notify_meeting_started`` would have written for a member.
    writer_bus = MessageBus(messages_dir=str(session_msg_dir))
    writer_bus.send(
        from_name="Meeting [test-meet]",
        to_name="Adrian [BA]",
        content="📋 Meeting started — your turn coming",
        message_type="meeting_started",
    )
    # Sanity: the file is where we expect.
    assert (session_msg_dir / "adrian__ba_inbox.jsonl").exists()

    # Build a fake registry with Adrian's stored env_vars matching
    # production (TEAM_MESSAGES_DIR present and session-scoped).
    fake_registry = MagicMock()
    fake_registry.has_running_resume.return_value = False
    fake_record = MagicMock()
    fake_record.run_id = "old-run-id"
    fake_record.original_config = {
        "agent_name": "Adrian [BA]",
        "role": "ba",
        "workspace_dir": str(workspace),
        "project_dir": str(backend_root),
        "env_vars": {
            "TEAM_WORKSPACE": str(workspace),
            "TEAM_SESSION_ID": session_id,
            "TEAM_MESSAGES_DIR": str(session_msg_dir),  # ← session-scoped
            "TEAM_MY_NAME": "Adrian [BA]",
        },
        "servers": [],
        "instruction": "",
        "task": "",
        "context": "",
        "model": "",
    }
    fake_registry.get.return_value = fake_record

    # Patch ``run_isolated_agent_background`` to verify the function
    # reached the spawn step (which means the inbox was correctly
    # located and ``unread`` was non-empty — the assertion that pins
    # the fix).
    spawn_called: dict = {}

    async def _fake_run_iso(*args, **kwargs):
        spawn_called["agent_name"] = kwargs.get("agent_name")
        spawn_called["task"] = kwargs.get("task")
        return "new-run-id"

    with patch(
        "fast_agent.spawn.isolated_spawner.run_isolated_agent_background",
        side_effect=_fake_run_iso,
    ):
        await _check_and_resume_on_inbox(
            run_id="old-run-id",
            agent_name="Adrian [BA]",
            registry=fake_registry,
            env_vars=fake_record.original_config["env_vars"],
        )

    assert spawn_called.get("agent_name") == "Adrian [BA]", (
        "Expected respawn to be triggered. Either the path resolution "
        "ignored TEAM_MESSAGES_DIR and read from the wrong folder "
        "(silent return on 0 unread), or another guard fired. "
        f"spawn_called={spawn_called!r}"
    )
    assert "📬 NEW MESSAGES" not in (spawn_called.get("task") or "") or "meeting_started" in (
        spawn_called.get("task") or ""
    ), "Inbox content should appear in the follow-up task"


@pytest.mark.asyncio
async def test_check_and_resume_warns_when_messages_dir_missing(tmp_path, caplog):
    """If no TEAM_MESSAGES_DIR and no workspace path can be resolved, the
    function must NOT exit silently — it must emit a WARNING explaining
    why the agent will not be respawned. This is the fail-loud contract:
    silent returns are how we wasted 20+ minutes diagnosing the retro
    meeting incident.
    """
    from fast_agent.spawn.isolated_spawner import _check_and_resume_on_inbox

    fake_registry = MagicMock()
    fake_registry.has_running_resume.return_value = False
    fake_record = MagicMock()
    fake_record.run_id = "old-run-id"
    fake_record.original_config = {"context": ""}  # no workspace path anywhere
    fake_registry.get.return_value = fake_record

    with caplog.at_level(logging.WARNING, logger="fast_agent.spawn.isolated_spawner"):
        await _check_and_resume_on_inbox(
            run_id="old-run-id",
            agent_name="Adrian [BA]",
            registry=fake_registry,
            env_vars={},  # neither TEAM_MESSAGES_DIR nor TEAM_WORKSPACE
        )

    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "Adrian [BA]" in r.getMessage()
    ]
    assert warnings, (
        f"Expected a WARNING log explaining why Adrian will not be "
        f"respawned. Actual records: {[(r.levelname, r.getMessage()) for r in caplog.records]}. "
        "Silent return = lost diagnose time when production breaks."
    )


@pytest.mark.asyncio
async def test_check_and_resume_warns_when_inbox_empty(tmp_path, caplog):
    """When messages_dir is correctly resolved but the inbox is empty
    (no unread), the function must log an INFO/WARNING line saying so.
    Previously this was a silent return. With fail-loud, future
    investigations can see exactly which agent was checked and found
    no work.
    """
    from fast_agent.spawn.isolated_spawner import _check_and_resume_on_inbox

    backend_root = tmp_path / "backend"
    session_id = "emptysid"
    session_msg_dir = backend_root / ".runtime" / "state" / "messages" / session_id
    session_msg_dir.mkdir(parents=True)
    # NB: NO inbox file written — bus.read_unread returns []

    fake_registry = MagicMock()
    fake_registry.has_running_resume.return_value = False
    fake_record = MagicMock()
    fake_record.run_id = "old-run-id"
    fake_record.original_config = {
        "env_vars": {"TEAM_MESSAGES_DIR": str(session_msg_dir)},
    }
    fake_registry.get.return_value = fake_record

    with caplog.at_level(logging.INFO, logger="fast_agent.spawn.isolated_spawner"):
        await _check_and_resume_on_inbox(
            run_id="old-run-id",
            agent_name="Adrian [BA]",
            registry=fake_registry,
            env_vars={"TEAM_MESSAGES_DIR": str(session_msg_dir)},
        )

    # We accept INFO or WARNING — the contract is "not silent".
    relevant = [
        r for r in caplog.records
        if "Adrian [BA]" in r.getMessage()
        and ("0 unread" in r.getMessage() or "no unread" in r.getMessage().lower()
             or "nothing to resume" in r.getMessage().lower())
    ]
    assert relevant, (
        f"Expected an INFO log saying Adrian has 0 unread / nothing to resume. "
        f"Got: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
