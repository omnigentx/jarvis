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

import asyncio
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


# ─── Phase 2: state machine + SSE timing ────────────────────────────


@pytest.fixture
def captured_sse(monkeypatch):
    """Capture SSE events emitted via activity_stream_manager.broadcast."""
    import services.activity_stream as act

    events: list[dict] = []

    class FakeManager:
        def broadcast(self, payload: dict) -> None:
            events.append(payload)

    fake = FakeManager()
    original = act.activity_stream_manager
    act.activity_stream_manager = fake
    yield events
    act.activity_stream_manager = original


def test_idle_pause_emits_both_pausing_and_paused(
    fresh_manager, fake_registry, captured_sse,
):
    """Idle agent (no in-flight turn) → pause() must emit BOTH
    ``agent_pausing`` AND ``agent_paused`` so the UI doesn't get stuck
    on the transitional "Pausing…" spinner forever.
    """
    fake_registry.find_by_name.return_value = []  # in-process, no subprocess pid

    fresh_manager.pause("Jarvis")

    types = [e["event_type"] for e in captured_sse]
    assert types == ["agent_pausing", "agent_paused"], types
    assert fresh_manager.state_of("Jarvis") == "paused"


def test_idle_resume_emits_both_resuming_and_resumed(
    fresh_manager, fake_registry, captured_sse,
):
    """Idle agent → resume() must emit BOTH ``agent_resuming`` AND
    ``agent_resumed`` so the UI can leave the "Resuming…" spinner.
    """
    fake_registry.find_by_name.return_value = []
    fresh_manager.pause("Jarvis")
    captured_sse.clear()

    fresh_manager.resume("Jarvis")

    types = [e["event_type"] for e in captured_sse]
    assert types == ["agent_resuming", "agent_resumed"], types
    assert fresh_manager.state_of("Jarvis") == "running"


def test_active_pause_emits_only_pausing(
    fresh_manager, fake_registry, captured_sse,
):
    """Active agent (in-flight turn) → pause() only emits ``agent_pausing``.
    The terminal ``agent_paused`` event comes later from the hook,
    when the agent reaches a checkpoint.
    """
    fake_registry.find_by_name.return_value = []
    # Simulate an in-flight turn — before_llm_call hook would have
    # flipped this to True.
    fresh_manager._active["Jarvis"] = True

    fresh_manager.pause("Jarvis")

    types = [e["event_type"] for e in captured_sse]
    assert types == ["agent_pausing"], types
    # State stays in 'pausing' until the hook fires the terminal transition.
    assert fresh_manager.state_of("Jarvis") == "pausing"


def test_subprocess_pause_does_not_double_emit_paused(
    fresh_manager, fake_registry, captured_sse, monkeypatch,
):
    """Subprocess agent (has PID) → main process emits ``agent_pausing``
    only. The terminal ``agent_paused`` is emitted by the subprocess
    itself from its own hook chain. If main process also emitted
    ``agent_paused``, UI would see duplicate events.
    """
    fake_registry.find_by_name.return_value = [{
        "run_id": "run-sub",
        "agent_name": "Dev",
        "status": "running",
        "pid": 99999,  # nonexistent — but os.kill(pid, 0) check matters
    }]
    # Make os.kill(pid, 0) succeed so _find_pid returns the pid, and
    # neutralize the SIGUSR1 send so the test doesn't actually signal.
    import services.pause_controller as pc

    monkeypatch.setattr(pc.os, "kill", lambda *a, **kw: None)

    fresh_manager.pause("Dev")

    types = [e["event_type"] for e in captured_sse]
    assert types == ["agent_pausing"], types
    assert fresh_manager.state_of("Dev") == "pausing"


def test_hook_emits_paused_when_blocked_then_resumed_when_woken():
    """Verify the in-hook state transitions:
    ``pausing → paused`` before ``await event.wait()``,
    ``resuming → running`` after the await returns.
    """
    from services.pause_controller import PauseController
    import services.activity_stream as act

    events: list[dict] = []

    class FakeMgr:
        def broadcast(self, p):
            events.append(p)

    original = act.activity_stream_manager
    act.activity_stream_manager = FakeMgr()

    async def run():
        ctrl = PauseController()
        hooks = ctrl.create_pause_hooks("HookAgent")

        # Simulate before_llm_call firing (flips _active to True).
        async def hook_phase():
            await hooks.before_llm_call(None, None)

        # Pause externally.
        ctrl._active["HookAgent"] = True
        ctrl.pause("HookAgent")
        # No 'agent_paused' yet — agent is "active" so emit is hook's job.
        assert [e["event_type"] for e in events] == ["agent_pausing"]
        events.clear()

        # Schedule the hook on a task; it should block at await event.wait().
        task = asyncio.create_task(hook_phase())
        await asyncio.sleep(0.05)  # let hook reach `await event.wait()`

        # Hook should have emitted 'agent_paused' and is now blocked.
        assert events[0]["event_type"] == "agent_paused"
        assert not task.done()
        events.clear()

        # Resume — hook wakes, emits agent_resumed.
        ctrl.resume("HookAgent")
        await task

        # Order: resume() emits agent_resuming, hook emits agent_resumed.
        types = [e["event_type"] for e in events]
        assert "agent_resuming" in types
        assert "agent_resumed" in types
        # Resuming must come before resumed.
        assert types.index("agent_resuming") < types.index("agent_resumed")

    try:
        asyncio.run(run())
    finally:
        act.activity_stream_manager = original


def test_state_of_defaults_to_running(fresh_manager):
    """Unknown agent → state_of returns 'running' (the safe default —
    UI treats unknown agents as idle/working, never as paused)."""
    assert fresh_manager.state_of("Unknown") == "running"


# ─── Phase 3: instant LLM cancel ────────────────────────────────────


def test_pause_cancels_in_flight_llm_task():
    """When the pause hook has captured a task ref (= agent is mid-LLM),
    ``pause()`` must call ``task.cancel()`` to interrupt the LLM stream.
    """
    from services.pause_controller import PauseController
    import services.activity_stream as act

    class FakeMgr:
        def broadcast(self, p): pass

    original = act.activity_stream_manager
    act.activity_stream_manager = FakeMgr()

    async def run():
        ctrl = PauseController()
        ctrl.create_pause_hooks("LLMer")

        # Simulate before_llm_call having captured a task. We use a
        # task that just sleeps, so we can assert it was cancelled.
        async def sleeper():
            await asyncio.sleep(60)

        task = asyncio.create_task(sleeper())
        # Let the task actually start running before we register it.
        await asyncio.sleep(0)
        ctrl._current_tasks["LLMer"] = task
        ctrl._active["LLMer"] = True

        ctrl.pause("LLMer")

        # Task should be cancelled. Awaiting it raises CancelledError.
        try:
            await task
            cancelled = False
        except asyncio.CancelledError:
            cancelled = True
        assert cancelled, "pause() must cancel the in-flight LLM task"

    try:
        asyncio.run(run())
    finally:
        act.activity_stream_manager = original


def test_pause_does_not_cancel_when_no_task_registered():
    """If no task ref is registered (agent is between LLM call and
    next tool / turn end), ``pause()`` must NOT crash — just clear the
    event and let cooperative checkpoint handle it.
    """
    from services.pause_controller import PauseController
    import services.activity_stream as act

    class FakeMgr:
        def broadcast(self, p): pass

    original = act.activity_stream_manager
    act.activity_stream_manager = FakeMgr()
    try:
        ctrl = PauseController()
        # _current_tasks is empty for "Idle". pause() must succeed.
        assert ctrl.pause("Idle") is True
        assert ctrl.is_paused("Idle") is True
    finally:
        act.activity_stream_manager = original


def test_on_pause_cancel_hook_returns_true_when_paused_then_resumed():
    """The on_pause_cancel hook awaits resume and returns True so the
    tool_runner retries the LLM call. Validates the contract that
    tool_runner.__anext__ uses to decide between retry and propagate.
    """
    from services.pause_controller import PauseController
    import services.activity_stream as act

    class FakeMgr:
        def broadcast(self, p): pass

    original = act.activity_stream_manager
    act.activity_stream_manager = FakeMgr()

    async def run():
        ctrl = PauseController()
        hooks = ctrl.create_pause_hooks("Retrier")
        # ``create_pause_hooks`` captured the event by reference, so
        # subsequent ``pause()``/``resume()`` calls toggle the SAME
        # event the hook awaits — that's the contract we rely on.

        ctrl.pause("Retrier")  # event cleared

        # The on_pause_cancel hook should block until we set the event,
        # then return True.
        task = asyncio.create_task(hooks.on_pause_cancel(runner=None))
        await asyncio.sleep(0.05)
        assert not task.done(), "on_pause_cancel must block while paused"

        ctrl.resume("Retrier")  # event set → hook wakes
        result = await task

        assert result is True, "must return True so runner retries the LLM call"

    try:
        asyncio.run(run())
    finally:
        act.activity_stream_manager = original


def test_on_pause_cancel_returns_false_when_not_paused():
    """If event is set (= agent not paused), on_pause_cancel returns False
    so the runner re-raises the CancelledError (genuine cancel, e.g.
    chat request was aborted client-side).
    """
    from services.pause_controller import PauseController
    import services.activity_stream as act

    class FakeMgr:
        def broadcast(self, p): pass

    original = act.activity_stream_manager
    act.activity_stream_manager = FakeMgr()

    async def run():
        ctrl = PauseController()
        hooks = ctrl.create_pause_hooks("Genuine")
        # event is set by default — not paused.
        result = await hooks.on_pause_cancel(runner=None)
        assert result is False

    try:
        asyncio.run(run())
    finally:
        act.activity_stream_manager = original


def test_after_llm_call_clears_task_ref_so_pause_during_tool_does_not_cancel():
    """Strategy B: pause during tool-call must not cancel anything (tool
    side-effects should complete). The after_llm_call hook clears
    ``_current_tasks`` so a subsequent pause has no task to cancel.
    """
    from services.pause_controller import PauseController
    import services.activity_stream as act

    class FakeMgr:
        def broadcast(self, p): pass

    original = act.activity_stream_manager
    act.activity_stream_manager = FakeMgr()

    async def run():
        ctrl = PauseController()
        hooks = ctrl.create_pause_hooks("ToolGuy")

        async def some_task():
            await asyncio.sleep(60)

        task = asyncio.create_task(some_task())
        await asyncio.sleep(0)

        # Simulate before_llm_call having captured this task.
        ctrl._current_tasks["ToolGuy"] = task

        # Simulate the LLM call finishing → after_llm_call fires.
        await hooks.after_llm_call(runner=None, message=None)

        # Now a pause during the tool phase should NOT cancel ``task``.
        assert "ToolGuy" not in ctrl._current_tasks
        ctrl.pause("ToolGuy")
        assert not task.done(), "tool-phase pause must not cancel the prior task"

        task.cancel()  # clean up

    try:
        asyncio.run(run())
    finally:
        act.activity_stream_manager = original
