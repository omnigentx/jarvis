"""Probe-then-dispatch contract for ``auto_wake_if_idle``.

The previous implementation used "Tier 1 → fall back to Tier 2" semantics:
first try the AgentChannel socket signal, and if it returns False try
the spawner instead. That hid a real ambiguity: ``send_signal`` returns
False on (sock missing) AND (connect refused) AND (write error) — three
very different states. The Tier 2 path then had to re-probe ``is_alive``
to avoid duplicate spawn when the process was actually alive but
mid-call.

The refactor enforces a single source of truth for liveness — one
``AgentChannel.is_alive`` probe — and dispatches to ONE strategy:

  - alive → ``_wake_alive_agent`` (socket signal)
  - dead  → ``_respawn_dead_agent`` (snapshot spawn)

No "fall back" anywhere. These tests pin the contract.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _restore_fast_agent_log_propagation():
    """``fast_agent.context.configure_logger`` sets ``propagate=False`` on
    the ``fast_agent`` logger when an agent's context boots (e.g. via an
    earlier e2e test that called ``agent.generate(...)``). That breaks
    ``caplog`` for ALL descendant loggers, including the one we assert
    against here. Force propagation on for the duration of the test.
    """
    fa_logger = logging.getLogger("fast_agent")
    prev = fa_logger.propagate
    fa_logger.propagate = True
    try:
        yield
    finally:
        fa_logger.propagate = prev


def test_alive_branch_calls_wake_strategy_only():
    """When liveness probe says alive, ``auto_wake_if_idle`` must take
    the wake-via-socket strategy and MUST NOT touch the respawn path
    (which would duplicate the live process)."""
    from fast_agent.spawn.servers._team_helpers import auto_wake_if_idle

    with patch(
        "fast_agent.spawn.agent_channel.AgentChannel.is_alive",
        return_value=True,
    ), patch(
        "fast_agent.spawn.agent_channel.AgentChannel.send_signal",
        return_value=True,
    ) as mock_signal, patch(
        "fast_agent.spawn.servers._team_helpers._respawn_dead_agent",
    ) as mock_respawn:
        auto_wake_if_idle("Adrian [BA]")

    mock_signal.assert_called_once_with("Adrian [BA]", "wake")
    mock_respawn.assert_not_called()


def test_dead_branch_calls_respawn_strategy_only():
    """When liveness probe says dead, ``auto_wake_if_idle`` must take
    the respawn-from-snapshot strategy and MUST NOT call send_signal
    (which would silently fail and waste a syscall)."""
    from fast_agent.spawn.servers._team_helpers import auto_wake_if_idle

    with patch(
        "fast_agent.spawn.agent_channel.AgentChannel.is_alive",
        return_value=False,
    ), patch(
        "fast_agent.spawn.agent_channel.AgentChannel.send_signal",
    ) as mock_signal, patch(
        "fast_agent.spawn.servers._team_helpers._respawn_dead_agent",
    ) as mock_respawn:
        auto_wake_if_idle("Adrian [BA]")

    mock_respawn.assert_called_once_with("Adrian [BA]")
    mock_signal.assert_not_called()


def test_alive_race_logs_warning_when_signal_fails_after_alive_probe(caplog):
    """If liveness says alive but ``send_signal`` returns False (race:
    process exited between probe and send), we MUST NOT silently
    respawn (the process could still be mid-shutdown and a respawn
    would duplicate it). The race is logged at WARNING so the next
    missed inbox can be correlated back to this event.
    """
    from fast_agent.spawn.servers._team_helpers import auto_wake_if_idle

    with patch(
        "fast_agent.spawn.agent_channel.AgentChannel.is_alive",
        return_value=True,
    ), patch(
        "fast_agent.spawn.agent_channel.AgentChannel.send_signal",
        return_value=False,
    ), patch(
        "fast_agent.spawn.servers._team_helpers._respawn_dead_agent",
    ) as mock_respawn, caplog.at_level(
        logging.WARNING, logger="fast_agent.spawn.servers._team_helpers"
    ):
        auto_wake_if_idle("Adrian [BA]")

    # Respawn must NOT be called — that's the whole point of the
    # liveness-first dispatch (no silent fall-through that could
    # duplicate a still-terminating process).
    mock_respawn.assert_not_called()

    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "Adrian [BA]" in r.getMessage()
    ]
    assert warnings, (
        "Expected a WARNING describing the alive→send-failed race. "
        f"Got: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )


def test_liveness_probe_failure_is_loud_and_aborts(caplog):
    """If the liveness probe itself raises (e.g. socket subsystem
    error), we MUST log at ERROR and refuse to act — neither strategy
    is safe without a reliable probe.
    """
    from fast_agent.spawn.servers._team_helpers import auto_wake_if_idle

    with patch(
        "fast_agent.spawn.agent_channel.AgentChannel.is_alive",
        side_effect=OSError("socket subsystem dead"),
    ), patch(
        "fast_agent.spawn.servers._team_helpers._wake_alive_agent",
    ) as mock_wake, patch(
        "fast_agent.spawn.servers._team_helpers._respawn_dead_agent",
    ) as mock_respawn, caplog.at_level(
        logging.ERROR, logger="fast_agent.spawn.servers._team_helpers"
    ):
        auto_wake_if_idle("Adrian [BA]")

    mock_wake.assert_not_called()
    mock_respawn.assert_not_called()
    errors = [
        r for r in caplog.records
        if r.levelname == "ERROR" and "Adrian [BA]" in r.getMessage()
    ]
    assert errors, (
        "Expected an ERROR when liveness probe raised. "
        f"Got: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )


def test_respawn_dead_agent_loud_when_no_registry(caplog):
    """``_respawn_dead_agent`` must log a WARNING when registry is
    unavailable — silently returning is how stale env vars in
    production cause invisible failures.
    """
    from fast_agent.spawn.servers import _team_helpers

    with patch.object(_team_helpers, "get_project_registry", return_value=None), \
            caplog.at_level(logging.WARNING, logger="fast_agent.spawn.servers._team_helpers"):
        _team_helpers._respawn_dead_agent("Adrian [BA]")

    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "Adrian [BA]" in r.getMessage()
        and "no registry" in r.getMessage().lower()
    ]
    assert warnings, "Expected WARNING about missing registry"


def test_respawn_dead_agent_loud_when_record_missing(caplog):
    """``_respawn_dead_agent`` must log a WARNING when the agent has
    no record in the registry — silently skipping was the 2026-05-15
    incident root cause.
    """
    from fast_agent.spawn.servers import _team_helpers

    fake_registry = MagicMock()
    fake_registry.find_by_name.return_value = None

    with patch.object(_team_helpers, "get_project_registry", return_value=fake_registry), \
            caplog.at_level(logging.WARNING, logger="fast_agent.spawn.servers._team_helpers"):
        _team_helpers._respawn_dead_agent("Adrian [BA]")

    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "Adrian [BA]" in r.getMessage()
        and "not found in registry" in r.getMessage().lower()
    ]
    assert warnings, "Expected WARNING about agent not in registry"
