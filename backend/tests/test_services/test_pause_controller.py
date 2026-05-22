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
):
    """End-to-end: pause an agent, simulate restart by creating a fresh
    PauseController, call restore_on_startup → the new controller knows
    the agent is paused (matches what was persisted).
    """
    fresh_manager.pause("Jarvis")
    assert fresh_manager.is_paused("Jarvis")

    # Verify the row exists in the table.
    db = isolated_pause_state_db.SessionLocal()
    try:
        rows = db.query(isolated_pause_state_db.AgentPauseStateModel).all()
        assert len(rows) == 1
        assert rows[0].agent_name == "Jarvis"
    finally:
        db.close()

    # Simulate restart: fresh controller, no in-memory state.
    from services.pause_controller import PauseController
    new_ctrl = PauseController()
    assert not new_ctrl.is_paused("Jarvis")

    restored = new_ctrl.restore_on_startup()

    assert restored == 1
    assert new_ctrl.is_paused("Jarvis"), \
        "restore must re-apply the pause from agent_pause_state"


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
):
    """Calling restore twice doesn't double-pause (or thrash SSE)."""
    fresh_manager.pause("PM")

    from services.pause_controller import PauseController
    new_ctrl = PauseController()
    first = new_ctrl.restore_on_startup()
    second = new_ctrl.restore_on_startup()

    assert first == 1
    assert second == 0  # already restored, no-op
    assert new_ctrl.is_paused("PM")


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
