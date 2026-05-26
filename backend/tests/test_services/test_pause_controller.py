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
import os
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
    """Stub ``services.shared_state.registry_db`` with a recording mock.

    Defaults make the agent look solo (no team): ``find_by_team_name``
    returns ``[]`` so the Phase 4 scope resolver picks the "solo agent"
    branch unless a specific test overrides it for team scenarios.
    """
    import services.shared_state as state

    original = state.registry_db
    fake = MagicMock()
    fake.upsert_record = MagicMock()
    fake.find_by_team_name = MagicMock(return_value=[])
    fake.find_by_name = MagicMock(return_value=[])
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


def test_subprocess_pause_emits_terminal_paused_immediately(
    fresh_manager, fake_registry, captured_sse, monkeypatch,
):
    """Backend emits BOTH ``agent_pausing`` and ``agent_paused`` for
    subprocess agents after SIGUSR1 is sent.

    Earlier design waited for the subprocess's own hook to emit
    ``agent_paused`` from spawn_events.sock — but the subprocess hook
    can fail to fire (subprocess dies before reaching checkpoint,
    socket drops, hook exception). Backend has all the info it needs
    (DB row written, signal sent) → emit ``agent_paused`` directly so
    UI doesn't get stuck on "Pausing…" forever. The subprocess hook
    may emit again later; FE handles that as an idempotent no-op.
    """
    fake_registry.find_by_name.return_value = [{
        "run_id": "run-sub",
        "agent_name": "Dev",
        "status": "running",
        "pid": 99999,  # nonexistent — but os.kill(pid, 0) check matters
    }]
    import services.pause_controller as pc

    monkeypatch.setattr(pc.os, "kill", lambda *a, **kw: None)

    fresh_manager.pause("Dev")

    types = [e["event_type"] for e in captured_sse]
    assert types == ["agent_pausing", "agent_paused"], types
    assert fresh_manager.state_of("Dev") == "paused"


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


# ─── Phase 4: team-wide pause + scope resolution ────────────────────


def test_pause_with_team_name_expands_to_all_members(fresh_manager, fake_registry, captured_sse):
    """``pause(team_name)`` resolves to every member in the team's
    registry and pauses each one. Implements requirement #4: pausing
    any member pauses the whole team.
    """
    fake_registry.find_by_team_name.return_value = [
        {"run_id": "r1", "agent_name": "PM",      "team_name": "AlphaTeam"},
        {"run_id": "r2", "agent_name": "Dev",     "team_name": "AlphaTeam"},
        {"run_id": "r3", "agent_name": "QA",      "team_name": "AlphaTeam"},
    ]

    result = fresh_manager.pause("AlphaTeam")

    assert result is True
    assert fresh_manager.is_paused("PM")
    assert fresh_manager.is_paused("Dev")
    assert fresh_manager.is_paused("QA")


def test_pause_on_member_expands_to_team(fresh_manager, fake_registry, captured_sse):
    """``pause(member_name)`` for an agent that belongs to a team
    expands to the whole team (members + orchestrator).
    """
    def by_team(name):
        if name == "AlphaTeam":
            return [
                {"run_id": "r1", "agent_name": "PM",  "team_name": "AlphaTeam"},
                {"run_id": "r2", "agent_name": "Dev", "team_name": "AlphaTeam"},
            ]
        return []

    def by_name(name):
        if name == "Dev":
            return [{"run_id": "r2", "agent_name": "Dev", "team_name": "AlphaTeam"}]
        return []

    fake_registry.find_by_team_name.side_effect = by_team
    fake_registry.find_by_name.side_effect = by_name

    fresh_manager.pause("Dev")

    assert fresh_manager.is_paused("Dev")
    assert fresh_manager.is_paused("PM"), \
        "pausing one member must propagate to the orchestrator/peers"


def test_pause_on_solo_agent_does_not_expand(fresh_manager, fake_registry, captured_sse):
    """Agents not in any team (in-process Jarvis, ad-hoc spawn) must
    NOT cause spurious DB lookups to leak into other agents — pausing
    Jarvis must not pause anything else.
    """
    fake_registry.find_by_team_name.return_value = []
    fake_registry.find_by_name.return_value = []  # not in registry — solo

    fresh_manager.pause("Jarvis")

    assert fresh_manager.is_paused("Jarvis")
    assert len(fresh_manager.get_all_paused()) == 1, \
        "solo pause must affect exactly one agent"


def test_resume_team_resumes_all_members(fresh_manager, fake_registry, captured_sse):
    """Mirror of pause — resume(team_name) must resume every member."""
    fake_registry.find_by_team_name.return_value = [
        {"run_id": "r1", "agent_name": "PM",  "team_name": "AlphaTeam"},
        {"run_id": "r2", "agent_name": "Dev", "team_name": "AlphaTeam"},
    ]

    fresh_manager.pause("AlphaTeam")
    assert fresh_manager.is_paused("PM")
    assert fresh_manager.is_paused("Dev")

    fresh_manager.resume("AlphaTeam")

    assert not fresh_manager.is_paused("PM")
    assert not fresh_manager.is_paused("Dev")


def test_is_team_paused_reports_true_when_any_member_paused(
    fresh_manager, fake_registry,
):
    """The late-joiner hook in spawn_progress_bridge calls
    ``is_team_paused`` to decide whether to auto-pause a new member.
    Pin the contract: True iff at least one team member is paused.
    """
    fake_registry.find_by_team_name.return_value = [
        {"run_id": "r1", "agent_name": "PM",  "team_name": "Beta"},
        {"run_id": "r2", "agent_name": "Dev", "team_name": "Beta"},
    ]

    assert fresh_manager.is_team_paused("Beta") is False

    fresh_manager.pause("Beta")
    assert fresh_manager.is_team_paused("Beta") is True

    fresh_manager.resume("Beta")
    assert fresh_manager.is_team_paused("Beta") is False


def test_is_team_paused_returns_false_for_non_team_name(fresh_manager, fake_registry):
    """``is_team_paused("Jarvis")`` (an in-process agent that is no team)
    must return False — guards against false-positive auto-pauses for
    solo agents that happen to share a name with an existing team.
    """
    fake_registry.find_by_team_name.return_value = []
    fake_registry.find_by_name.return_value = []
    assert fresh_manager.is_team_paused("Jarvis") is False


# ─── Phase 5: auto-attach ────────────────────────────────────────────


class _FakeAgent:
    """Stand-in for fast-agent's McpAgent — only what attach() reads."""

    def __init__(self, name, existing_hooks=None):
        self.name = name
        self.tool_runner_hooks = existing_hooks


def test_attach_merges_pause_hooks_when_no_existing(fresh_manager):
    """Agent with no prior hooks → after attach, ``tool_runner_hooks``
    is the controller's pause hooks (verbatim — no merge wrapper)."""
    agent = _FakeAgent("Solo")
    fresh_manager.attach(agent)

    hooks = agent.tool_runner_hooks
    assert hooks is not None
    assert hooks.before_llm_call is not None
    assert hooks.on_pause_cancel is not None  # Phase 3 contract
    assert agent._pause_attached is True


def test_attach_merges_with_existing_hooks(fresh_manager):
    """When agent already has hooks (progress/RTAC/card-defined), attach
    merges pause hooks alongside via merge_hooks — both pre-existing
    and pause hooks must fire on every callback.
    """
    from fast_agent.agents.tool_runner import ToolRunnerHooks

    progress_fired = []

    async def progress_before_llm(runner, messages):
        progress_fired.append("progress_before_llm")

    progress_hooks = ToolRunnerHooks(before_llm_call=progress_before_llm)
    agent = _FakeAgent("Merged", existing_hooks=progress_hooks)

    fresh_manager.attach(agent)

    async def run():
        await agent.tool_runner_hooks.before_llm_call(None, None)

    asyncio.run(run())

    assert "progress_before_llm" in progress_fired, \
        "merged hook must still invoke the original progress hook"


def test_attach_is_idempotent(fresh_manager):
    """Calling attach twice on the same agent without detach in between
    must NOT double-merge pause hooks (would create two separate
    captures of ``_current_tasks`` writing to the same dict key — wasted
    work + confusing log spam).
    """
    agent = _FakeAgent("Twice")

    fresh_manager.attach(agent)
    hooks_after_first = agent.tool_runner_hooks

    fresh_manager.attach(agent)
    hooks_after_second = agent.tool_runner_hooks

    assert hooks_after_first is hooks_after_second, \
        "second attach must be no-op (same hooks object)"


def test_detach_resets_activity_so_subsequent_pause_emits_terminal_event(
    fresh_manager, fake_registry, captured_sse,
):
    """Regression for 2026-05-24 stuck-Pausing bug.

    If ``before_llm_call`` fires (setting ``_active=True``) but the
    corresponding ``after_turn_complete`` doesn't (request cancelled
    mid-turn, exception before the final hook), ``_active['name']``
    leaks to True. A later manual ``pause()`` sees ``_active=True`` →
    skips the idle-emit branch → UI is stuck on "Pausing…" forever
    because no hook will ever fire to emit the terminal ``agent_paused``.

    ``detach()`` must reset ``_active`` so the next request's hook
    chain starts clean and a manual pause-while-no-chat correctly
    treats the agent as idle.
    """
    agent = _FakeAgent("Jarvis")

    # Simulate a chat request whose before_llm_call set _active.
    fresh_manager._active["Jarvis"] = True
    fresh_manager._current_tasks["Jarvis"] = object()  # any non-None
    fresh_manager.attach(agent)

    # Request ends — chat.py restores hooks + detaches.
    fresh_manager.detach(agent)

    assert "Jarvis" not in fresh_manager._active, \
        "detach must reset _active so a subsequent pause sees idle"
    assert "Jarvis" not in fresh_manager._current_tasks

    # Now user clicks pause. With _active leaked-but-cleared by detach,
    # pause emits the terminal agent_paused (idle branch).
    fresh_manager.pause("Jarvis")
    types = [e["event_type"] for e in captured_sse]
    assert "agent_pausing" in types
    assert "agent_paused" in types, (
        "post-detach pause on idle agent must emit terminal agent_paused, "
        "not stop at agent_pausing"
    )


def test_detach_preserves_pause_state(fresh_manager, fake_registry, captured_sse):
    """Counterpoint: detach is per-request lifecycle, NOT a state wipe.
    A user who paused an agent BEFORE the chat request closes must
    stay paused after detach — otherwise pause/resume would race with
    chat request lifecycle.
    """
    agent = _FakeAgent("Jarvis")
    fresh_manager.attach(agent)
    fresh_manager.pause("Jarvis")
    assert fresh_manager.is_paused("Jarvis")

    fresh_manager.detach(agent)

    assert fresh_manager.is_paused("Jarvis"), \
        "manual pause must survive request-scoped detach"


def test_detach_clears_sentinel_so_attach_re_wires(fresh_manager):
    """After detach, a subsequent attach must take effect — this is the
    chat.py per-request lifecycle: snapshot original → attach → restore
    original → detach → next request can attach again.
    """
    agent = _FakeAgent("Cycle")

    fresh_manager.attach(agent)
    assert agent._pause_attached is True

    # Simulate chat.py's restore: hooks go back to original (None here).
    agent.tool_runner_hooks = None
    fresh_manager.detach(agent)
    assert not hasattr(agent, "_pause_attached")

    fresh_manager.attach(agent)
    assert agent._pause_attached is True
    assert agent.tool_runner_hooks is not None, \
        "re-attach must wire hooks back onto the restored state"


def test_attach_skips_agent_without_name(fresh_manager, caplog):
    """Defensive guard: agent objects without ``.name`` get logged and
    skipped, not crashed (some test mocks / partial objects could trip
    this; PauseController must not abort the whole hook wiring).
    """
    class Nameless:
        tool_runner_hooks = None

    agent = Nameless()
    fresh_manager.attach(agent)  # must not raise

    assert getattr(agent, "_pause_attached", False) is False


# ─── Phase 6: restart recovery ──────────────────────────────────────


@pytest.fixture
def isolated_pause_state_db():
    """Truncate ``agent_pause_state`` before and after each test so rows
    don't bleed between cases.

    Uses the session-shared test DB set up in tests/conftest.py — much
    simpler than reloading core.database (which fights the global
    JARVIS_DB_PATH override).
    """
    import core.database as _db

    # Ensure table exists (handles fresh test sessions before any other
    # fixture has touched the DB).
    _db.Base.metadata.create_all(_db.engine)

    def _wipe():
        db = _db.SessionLocal()
        try:
            db.query(_db.AgentPauseStateModel).delete()
            db.commit()
        finally:
            db.close()

    _wipe()
    yield _db
    _wipe()


def test_pause_persists_then_restore_re_pauses(
    isolated_pause_state_db, fresh_manager, fake_registry, captured_sse,
    monkeypatch,
):
    """End-to-end: pause a subprocess agent whose process survives
    the backend restart, simulate restart by creating a fresh
    PauseController, call restore_on_startup → the new controller
    knows the agent is paused.

    Only subprocess agents with live PIDs are eligible for restore —
    in-process agents are orphan (their chat task died with the
    backend). The previous version of this test used Jarvis (no
    spawn_record), which is now correctly dropped on restore; this
    test moved to a subprocess agent to keep the happy path covered.
    """
    # Pretend "Dev" is a subprocess whose PID survives. ``os.kill(pid, 0)``
    # against ourselves always succeeds — gives us a "live" PID without
    # spawning anything.
    own_pid = os.getpid()
    fake_registry.find_by_name.return_value = [
        {"run_id": "r1", "agent_name": "Dev", "pid": own_pid, "status": "running"}
    ]
    # Suppress the actual signal send so we don't SIGUSR1 our own pytest.
    import services.pause_controller as pc
    monkeypatch.setattr(pc.os, "kill", lambda *a, **kw: None)

    fresh_manager.pause("Dev")
    assert fresh_manager.is_paused("Dev")

    db = isolated_pause_state_db.SessionLocal()
    try:
        rows = db.query(isolated_pause_state_db.AgentPauseStateModel).all()
        assert len(rows) == 1
        assert rows[0].agent_name == "Dev"
    finally:
        db.close()

    # Simulate restart: fresh controller, no in-memory state.
    new_ctrl = pc.PauseController()
    assert not new_ctrl.is_paused("Dev")

    restored = new_ctrl.restore_on_startup()

    assert restored == 1
    assert new_ctrl.is_paused("Dev"), \
        "subprocess pause with live PID must be restored across restart"


def test_resume_deletes_pause_state_row(
    isolated_pause_state_db, fresh_manager, fake_registry, captured_sse,
):
    """resume() must drop the row so a subsequent restart doesn't
    spuriously re-pause an agent the user has already resumed.
    """
    fresh_manager.pause("Dev")
    fresh_manager.resume("Dev")

    db = isolated_pause_state_db.SessionLocal()
    try:
        rows = db.query(isolated_pause_state_db.AgentPauseStateModel).all()
        assert len(rows) == 0, "resume must delete the persisted pause row"
    finally:
        db.close()


def test_restore_is_idempotent(
    isolated_pause_state_db, fresh_manager, fake_registry, captured_sse,
    monkeypatch,
):
    """Calling restore twice doesn't double-pause (or thrash SSE).
    Uses a subprocess-style agent (live PID) because in-process
    rows are dropped on restore.
    """
    own_pid = os.getpid()
    fake_registry.find_by_name.return_value = [
        {"run_id": "r1", "agent_name": "PM", "pid": own_pid, "status": "running"}
    ]
    import services.pause_controller as pc
    monkeypatch.setattr(pc.os, "kill", lambda *a, **kw: None)

    fresh_manager.pause("PM")

    new_ctrl = pc.PauseController()
    first = new_ctrl.restore_on_startup()
    second = new_ctrl.restore_on_startup()

    assert first == 1
    assert second == 0  # already restored, no-op
    assert new_ctrl.is_paused("PM")


def test_restore_drops_dead_subprocess_rows(
    isolated_pause_state_db, fake_registry, captured_sse,
):
    """jarvis#48 F3: restore_on_startup must GC ``agent_pause_state``
    rows whose subprocess PID is dead — otherwise stale rows accumulate
    across restart cycles and a future spawn under the same agent_name
    would inherit a "paused" state it can't escape (no live subprocess
    to receive SIGUSR2).

    Setup: write a row directly to the DB (simulating a previous
    backend session) for an agent whose ``spawn_records`` row has a
    PID that has since died. After restore, the row should be gone.
    """
    # Pre-seed: subprocess agent's pause row from the "previous" run.
    db = isolated_pause_state_db.SessionLocal()
    try:
        db.add(isolated_pause_state_db.AgentPauseStateModel(
            agent_name="DeadDev",
            paused_at=1000.0,
            team_name="ZombieTeam",
            reason="manual",
        ))
        db.commit()
    finally:
        db.close()

    # Fake registry: DeadDev has a spawn_record with PID 999999 (never
    # going to be alive on this host — os.kill(0) will raise).
    fake_registry.find_by_name.return_value = [
        {"run_id": "r-dead", "agent_name": "DeadDev", "pid": 999999},
    ]

    from services.pause_controller import PauseController
    ctrl = PauseController()
    restored = ctrl.restore_on_startup()

    assert restored == 0, "dead-PID rows must not count as restored"
    assert not ctrl.is_paused("DeadDev"), \
        "dead subprocess must not be in _paused_agents (can't be resumed)"

    db = isolated_pause_state_db.SessionLocal()
    try:
        rows = db.query(isolated_pause_state_db.AgentPauseStateModel).all()
        assert len(rows) == 0, "dead-PID row must be GC'd during restore"
    finally:
        db.close()


def test_restore_drops_in_process_agent_rows(
    isolated_pause_state_db, fake_registry, captured_sse,
):
    """In-process agents (Jarvis, no ``spawn_records`` row) — their
    pause is tied to an HTTP chat request. Backend restart kills the
    request → no work to resume → pause is orphan.

    User's mental model (2026-05-24 feedback): "pause then resume
    must continue the work; if it goes idle, what's the point of
    pause?". Restoring an in-process agent's pause across restart
    would resume to nothing — the user's exact complaint. So drop
    the row instead.
    """
    db = isolated_pause_state_db.SessionLocal()
    try:
        db.add(isolated_pause_state_db.AgentPauseStateModel(
            agent_name="Jarvis",
            paused_at=1000.0,
            reason="manual",
        ))
        db.commit()
    finally:
        db.close()

    fake_registry.find_by_name.return_value = []  # in-process

    from services.pause_controller import PauseController
    ctrl = PauseController()
    restored = ctrl.restore_on_startup()

    assert restored == 0, "in-process pause must NOT be restored"
    assert not ctrl.is_paused("Jarvis")

    # Row must be GC'd from agent_pause_state.
    db = isolated_pause_state_db.SessionLocal()
    try:
        rows = db.query(isolated_pause_state_db.AgentPauseStateModel).all()
        assert len(rows) == 0, \
            "orphan in-process pause row must be cleaned up on restore"
    finally:
        db.close()


def test_team_pause_does_not_run_n_plus_1_team_lookup(
    isolated_pause_state_db, fresh_manager, fake_registry, captured_sse,
):
    """jarvis#48 F4: when pausing a team scope, the team_name is already
    known from ``_resolve_scope`` — ``_persist_pause`` must not re-run
    ``find_by_name`` per member just to re-discover the same team
    affiliation. With N members, the old code did N extra DB queries
    on top of the 1 ``find_by_team_name`` call.

    Asserts: after ``pause(team_name)`` for 3 members, the registry's
    ``find_by_name`` is called at most once per member from path
    *other than* ``_persist_pause`` (i.e. from ``_find_pid`` and
    ``_update_db_status``). Specifically ``_team_of`` must not fire
    because the hint flows through from ``pause()`` → ``_pause_one``
    → ``_persist_pause``.
    """
    members = [
        {"run_id": "r1", "agent_name": "PM",  "team_name": "AlphaTeam"},
        {"run_id": "r2", "agent_name": "Dev", "team_name": "AlphaTeam"},
        {"run_id": "r3", "agent_name": "QA",  "team_name": "AlphaTeam"},
    ]
    fake_registry.find_by_team_name.return_value = members

    # Side-effect lookup by name returns the per-agent row (used by
    # _find_pid and _update_db_status — both are unavoidable per-member).
    def by_name(name):
        return [m for m in members if m["agent_name"] == name]
    fake_registry.find_by_name.side_effect = by_name

    fresh_manager.pause("AlphaTeam")

    # 1 from _resolve_scope (find_by_team_name, not find_by_name) +
    # 2 per member (_find_pid for SIGUSR1, _update_db_status for DB).
    # If the N+1 regressed, _persist_pause adds a 3rd per-member call.
    by_name_calls = fake_registry.find_by_name.call_count
    assert by_name_calls == 2 * len(members), (
        f"expected 2 find_by_name per member ({2*len(members)} total), "
        f"got {by_name_calls} — _team_of fired = N+1 regression"
    )


def test_team_pause_persists_team_name_on_agent_pause_state_row(
    isolated_pause_state_db, fresh_manager, fake_registry, captured_sse,
):
    """Counterpoint to the F4 test: even with the hint flowing through,
    the persisted row's ``team_name`` column must still get populated
    correctly — otherwise restart recovery loses the team affiliation
    and ``is_team_paused`` queries break.
    """
    members = [
        {"run_id": "r1", "agent_name": "PM",  "team_name": "BetaTeam"},
        {"run_id": "r2", "agent_name": "Dev", "team_name": "BetaTeam"},
    ]
    fake_registry.find_by_team_name.return_value = members

    def by_name(name):
        return [m for m in members if m["agent_name"] == name]
    fake_registry.find_by_name.side_effect = by_name

    fresh_manager.pause("BetaTeam")

    db = isolated_pause_state_db.SessionLocal()
    try:
        rows = db.query(isolated_pause_state_db.AgentPauseStateModel).all()
        by_agent = {r.agent_name: r for r in rows}
        assert set(by_agent) == {"PM", "Dev"}
        for row in rows:
            assert row.team_name == "BetaTeam", \
                f"team_name not persisted for {row.agent_name}"
    finally:
        db.close()


def test_e2e_sse_event_order_pause_resume_cycle(
    fresh_manager, fake_registry, captured_sse,
):
    """jarvis#48 F6: SSE event ordering across a full pause→resume
    cycle. The unit tests already cover individual transitions; this
    one pins the END-TO-END sequence that the dashboard relies on for
    the 4-state badge animation.

    For an idle agent (no in-flight turn), the controller emits the
    terminal event itself, so the full sequence is:

        agent_pausing → agent_paused → agent_resuming → agent_resumed
    """
    fresh_manager.pause("Jarvis")
    fresh_manager.resume("Jarvis")

    types = [e["event_type"] for e in captured_sse]

    assert types == [
        "agent_pausing", "agent_paused",
        "agent_resuming", "agent_resumed",
    ], f"event sequence regression: {types}"

    # Belt-and-suspenders: each event's ``data.status`` matches its phase.
    expected_status = {
        "agent_pausing": "pausing",
        "agent_paused": "paused",
        "agent_resuming": "resuming",
        "agent_resumed": "running",
    }
    for evt in captured_sse:
        assert evt["data"]["status"] == expected_status[evt["event_type"]], evt


def test_late_joiner_starts_paused_when_team_already_paused(
    fresh_manager, fake_registry, captured_sse,
):
    """Scenario: team is paused, a new member spawns. The spawn bridge
    is expected to call ``pause_controller.pause(new_name)`` for the
    joiner. Because the joiner's spawn_record is now in the registry,
    scope resolution treats it as a team member and the pause is
    effective.
    """
    initial = [
        {"run_id": "r1", "agent_name": "PM",  "team_name": "Gamma"},
        {"run_id": "r2", "agent_name": "Dev", "team_name": "Gamma"},
    ]
    fake_registry.find_by_team_name.return_value = list(initial)

    fresh_manager.pause("Gamma")
    assert fresh_manager.is_team_paused("Gamma") is True

    # Simulate spawn: new member added to the team registry.
    fake_registry.find_by_team_name.return_value = list(initial) + [
        {"run_id": "r3", "agent_name": "QA", "team_name": "Gamma"},
    ]
    fake_registry.find_by_name.return_value = [
        {"run_id": "r3", "agent_name": "QA", "team_name": "Gamma"},
    ]

    # Spawn bridge would do this:
    if fresh_manager.is_team_paused("Gamma"):
        fresh_manager.pause("QA")

    assert fresh_manager.is_paused("QA"), \
        "late joiner must start paused so it can't slip past the pause window"


# ─── Regression: uv-launcher PID problem (2026-05-24 Jordan-dies bug) ──


def test_find_pid_returns_python_child_not_uv_launcher(
    fresh_manager, fake_registry, monkeypatch,
):
    """spawn_registry stores the ``uv run python ...`` launcher PID,
    but SIGUSR1/SIGUSR2 handlers live in the python interpreter (one
    process deeper). Signaling uv directly kills it (default SIGUSR1
    action = TERMINATE) and orphans/kills the python child — the
    "Jordan dies on pause" bug.

    ``_find_pid`` must walk children and return the python interpreter
    PID. Falls back to uv PID only when no child is discoverable.
    """
    UV_PID = 99001
    PY_PID = 99002

    fake_registry.find_by_name.return_value = [{
        "run_id": "r-uv-test", "agent_name": "Subproc", "pid": UV_PID,
    }]
    import services.pause_controller as pc
    monkeypatch.setattr(pc.os, "kill", lambda *a, **kw: None)

    import subprocess as _subprocess

    def fake_run(cmd, **kwargs):
        # pgrep -P UV_PID → returns PY_PID
        if cmd[:2] == ["pgrep", "-P"] and cmd[2] == str(UV_PID):
            return _subprocess.CompletedProcess(cmd, 0, stdout=f"{PY_PID}\n", stderr="")
        # ps -p PY_PID -o comm= → python3
        if cmd[:2] == ["ps", "-p"]:
            return _subprocess.CompletedProcess(cmd, 0, stdout="python3.13\n", stderr="")
        return _subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(pc.PauseController._find_python_child.__wrapped__ if hasattr(
        pc.PauseController._find_python_child, '__wrapped__') else _subprocess, "run", fake_run)
    # The above monkeypatch indirection is ugly; do it directly on subprocess.run
    monkeypatch.setattr("subprocess.run", fake_run)

    pid = fresh_manager._find_pid("Subproc")
    assert pid == PY_PID, (
        f"_find_pid must return python child PID ({PY_PID}), "
        f"not uv launcher ({UV_PID}). Got {pid}."
    )


def test_find_pid_refuses_when_no_python_child_discoverable(
    fresh_manager, fake_registry, monkeypatch,
):
    """M5 fix (PR #49 review): if pgrep retries exhaust and no child
    is found (spawn race window before uv has fork'd python), refuse
    by returning None instead of falling back to the uv launcher PID.

    Falling back to uv_pid re-introduced the original bug this whole
    walk exists to prevent: SIGUSR1's default action is TERMINATE,
    uv has no SIGUSR1 handler, so signaling uv kills it and orphans
    the python child → entire agent dies on what was supposed to be
    a cooperative pause.

    Caller surfaces this as an actionable "agent still spawning,
    retry in a moment" — preferable to a silent crash.
    """
    UV_PID = 99003
    fake_registry.find_by_name.return_value = [{
        "run_id": "r-fallback", "agent_name": "Subproc", "pid": UV_PID,
    }]
    import services.pause_controller as pc
    monkeypatch.setattr(pc.os, "kill", lambda *a, **kw: None)

    # pgrep returns no children — simulates the post-uv-launch /
    # pre-python-fork race window. _find_python_child retries 5x with
    # 50ms backoff then gives up; we patch time.sleep to make the
    # test instant.
    import subprocess as _subprocess
    monkeypatch.setattr("subprocess.run", lambda *a, **kw:
        _subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""))
    monkeypatch.setattr("time.sleep", lambda _s: None)

    pid = fresh_manager._find_pid("Subproc")
    assert pid is None, (
        "no python child after retries → must refuse rather than "
        "signal the uv launcher (default-action=TERMINATE would "
        "kill the agent)"
    )


# ─── E2E: real uv subprocess + signal delivery ────────────────────


@pytest.mark.skipif(
    __import__("shutil").which("uv") is None or __import__("shutil").which("pgrep") is None,
    reason="requires uv + pgrep on PATH (macOS / Linux)",
)
def test_find_pid_walks_uv_to_python_child_with_real_subprocess(tmp_path):
    """Real-process integration test for the 2026-05-24 "spawn dies on
    pause" bug. Spawns ``uv run python -c '...'`` (matches how
    isolated_spawner.py creates agent subprocesses), installs a real
    SIGUSR1 handler in the python child that writes a marker file,
    then verifies:

    1. ``_find_python_child(uv_pid)`` returns the python child's PID,
       NOT the uv launcher's PID.
    2. Sending SIGUSR1 to that PID delivers to the python handler
       (marker file appears).
    3. The uv launcher is STILL alive after the signal — i.e. the
       handler ran instead of killing the process tree.

    Without ``_find_python_child``, signalling uv directly hits its
    default SIGUSR1 handler = TERMINATE → entire process tree dies →
    agent is gone instead of paused.
    """
    import os
    import signal
    import subprocess
    import time
    from services.pause_controller import PauseController

    marker = tmp_path / "sigusr1_received.txt"
    pause_marker = tmp_path / "paused.txt"
    script = f"""
import signal, time, sys
def on_sigusr1(signum, frame):
    open({str(marker)!r}, "w").write("got_sigusr1")
    open({str(pause_marker)!r}, "w").write("blocked")
signal.signal(signal.SIGUSR1, on_sigusr1)
print("READY", flush=True)
# Block on a sleep — handler runs, then continue sleeping. The test
# only cares that we didn't die from SIGUSR1.
for _ in range(50):
    time.sleep(0.1)
"""
    proc = subprocess.Popen(
        ["uv", "run", "python", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait until python child has started + installed handler.
        proc.stdout.readline()  # consume "READY\n"
        # Give pgrep a moment to see the child appear.
        time.sleep(0.5)

        uv_pid = proc.pid
        python_pid = PauseController._find_python_child(uv_pid)

        assert python_pid is not None, (
            "pgrep returned no children of uv launcher — spawn structure "
            "may have changed; reread isolated_spawner.py:189-194"
        )
        assert python_pid != uv_pid, (
            f"_find_python_child must return the python child PID, "
            f"not the uv launcher PID ({uv_pid})"
        )

        # Send SIGUSR1 to python child — handler should fire.
        os.kill(python_pid, signal.SIGUSR1)

        # Wait for marker file (handler ran).
        deadline = time.time() + 3.0
        while time.time() < deadline and not marker.exists():
            time.sleep(0.05)
        assert marker.exists(), (
            "SIGUSR1 to python child must reach the handler (marker file)"
        )

        # Critical: process tree must still be alive — the bug killed it.
        try:
            os.kill(uv_pid, 0)
        except ProcessLookupError:
            pytest.fail(
                "uv launcher died after SIGUSR1 to its python child — "
                "process tree should be intact"
            )
        try:
            os.kill(python_pid, 0)
        except ProcessLookupError:
            pytest.fail(
                "python child died after handling SIGUSR1 — handler "
                "must not terminate the process"
            )
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.skipif(
    __import__("shutil").which("uv") is None,
    reason="requires uv on PATH",
)
def test_uv_run_creates_two_level_process_tree(tmp_path):
    """Documents the structural invariant the bug fix depends on:
    ``uv run python ...`` creates a TWO-process tree (uv parent +
    python child), not a single-process exec. ``_find_python_child``
    is only meaningful if this shape holds.

    If a future uv version switches to exec semantics (replaces uv
    with python in-place), this test regresses loudly → prompts a
    re-read of isolated_spawner.py:189-194 to decide whether
    _find_python_child is still needed.
    """
    import subprocess
    import time

    proc = subprocess.Popen(
        ["uv", "run", "python", "-c", "import time; time.sleep(20)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.0)  # uv resolves deps + execs python
        out = subprocess.run(
            ["pgrep", "-P", str(proc.pid)],
            capture_output=True, text=True, timeout=2.0,
        )
        children = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
        assert len(children) >= 1, (
            "uv run no longer forks a python child — _find_python_child "
            "needs to be revisited. Check uv version + spawn semantics."
        )
        ps = subprocess.run(
            ["ps", "-p", str(children[0]), "-o", "comm="],
            capture_output=True, text=True, timeout=2.0,
        )
        assert "python" in ps.stdout.lower(), (
            f"uv's first child is not python: {ps.stdout!r}"
        )
    finally:
        proc.terminate()
        proc.wait(timeout=5)
