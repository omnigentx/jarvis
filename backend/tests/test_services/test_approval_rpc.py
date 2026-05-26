"""Tests for approval RPC migration.

Two layers:

1. ``ApprovalService.wait_for_resolution`` direct calls — covers the
   in-process pub/sub correctness (resolve-before-wait, concurrent
   waiters, not-found, signal-after-wait).
2. UDS round-trip through ``RuntimeRpcServer`` with the registered
   ``approval.*`` handlers — proves the full path the MCP subprocess
   exercises in production.

No mocks of the transport or the service.  We use the real SessionLocal
(rolled back per-test via ``mcp_db_isolation``) and a real Unix socket.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services import approval_rpc_handlers, approval_service as approval_module
from services.approval_service import approval_service
from services.runtime_rpc import RuntimeRpcServer
from tools.runtime_rpc_client import RuntimeRpcClient


@pytest.fixture(autouse=True)
def _isolate_db(mcp_db_isolation):
    """Every test runs inside a SAVEPOINT so approval rows it inserts
    are rolled back. Avoids leaking pending approvals between tests.
    """
    yield


@pytest.fixture(autouse=True)
def _isolate_pause_state():
    """PauseController is a module-level singleton — clear its in-memory
    state between tests so a pause from test A doesn't leak into test B.
    The DB-side ``agent_pause_state`` is handled by mcp_db_isolation's
    SAVEPOINT, but the controller's dicts/sets are not transactional.
    """
    from services.pause_controller import pause_controller
    # Snapshot what was there before (defensive — should be empty).
    snap_paused = set(pause_controller._paused_agents)
    snap_state = dict(pause_controller._agent_state)
    snap_active = dict(pause_controller._active)
    yield
    # Reset to pre-test state.
    pause_controller._paused_agents = snap_paused
    pause_controller._agent_state = snap_state
    pause_controller._active = snap_active
    pause_controller._current_tasks.clear()
    pause_controller._events.clear()


@pytest.fixture
def _stub_pause_manager(monkeypatch):
    """OPT-IN no-op for pause/resume. Used by tests that only exercise
    the wait/resolve pub-sub path and don't care whether the agent is
    actually marked paused. Happy-path tests deliberately DO NOT use
    this fixture — they need to verify pause_controller actually
    updates ``_paused_agents`` so a regression like the LLM team_name
    mismatch bug surfaces at test time, not user time.
    """
    from services import pause_manager as pm
    monkeypatch.setattr(pm.pause_manager, "pause", lambda *a, **k: True)
    monkeypatch.setattr(pm.pause_manager, "resume", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def _silence_activity_stream(monkeypatch):
    """SSE broadcast targets the live activity stream manager. Stub
    it so we don't pollute test logs and don't depend on background
    asyncio tasks the manager spawns at import time.
    """
    from services import activity_stream as a
    monkeypatch.setattr(a.activity_stream_manager, "broadcast", lambda *a, **k: None)


@pytest.fixture()
def _empty_waiters():
    """The pub/sub dict is module-scoped; reset between tests to keep
    waiter assertions deterministic when run alongside other tests."""
    approval_module._resolution_waiters.clear()
    yield
    approval_module._resolution_waiters.clear()


def _create_pending_approval() -> str:
    """Insert an approval row and return its id. Bypasses the public
    create_approval to avoid SSE/pause side effects in tests that only
    care about the wait/resolve path.
    """
    row = approval_service.create_approval({
        "agent_name": "test_agent",
        "title": "Test approval",
        "content": "Body",
    })
    return row["id"]


# ----- Direct service-level pub/sub ------------------------------------


@pytest.mark.asyncio
async def test_wait_returns_immediately_when_already_resolved(_empty_waiters):
    approval_id = _create_pending_approval()
    approval_service.resolve_approval(approval_id, "approve", "looks good")

    # No subscriber should be registered after a fast-path return.
    result = await approval_service.wait_for_resolution(approval_id)

    assert result["status"] == "approved"
    assert result["user_decision"] == "approve"
    assert result["user_comment"] == "looks good"
    assert approval_id not in approval_module._resolution_waiters


@pytest.mark.asyncio
async def test_wait_blocks_then_returns_on_resolve(_empty_waiters):
    approval_id = _create_pending_approval()

    # Resolve from a parallel task after the waiter has subscribed.
    async def _resolve_after_delay():
        # Yield so the waiter actually reaches ``await fut``.
        await asyncio.sleep(0.05)
        approval_service.resolve_approval(approval_id, "reject", "no thanks")

    waiter = asyncio.create_task(approval_service.wait_for_resolution(approval_id))
    resolver = asyncio.create_task(_resolve_after_delay())

    result = await asyncio.wait_for(waiter, timeout=2.0)
    await resolver

    assert result["status"] == "rejected"
    assert result["user_comment"] == "no thanks"
    # Cleanup happened in the finally block.
    assert approval_id not in approval_module._resolution_waiters


@pytest.mark.asyncio
async def test_unknown_approval_id_raises_keyerror(_empty_waiters):
    with pytest.raises(KeyError):
        await approval_service.wait_for_resolution("nonexistent-id")


@pytest.mark.asyncio
async def test_multiple_waiters_all_notified(_empty_waiters):
    """Team scenarios: more than one agent can wait on the same approval.
    Resolve must signal every subscriber.
    """
    approval_id = _create_pending_approval()

    waiters = [
        asyncio.create_task(approval_service.wait_for_resolution(approval_id))
        for _ in range(3)
    ]

    # Yield until all three have subscribed.
    for _ in range(50):
        if len(approval_module._resolution_waiters.get(approval_id, [])) == 3:
            break
        await asyncio.sleep(0.01)
    assert len(approval_module._resolution_waiters[approval_id]) == 3

    approval_service.resolve_approval(approval_id, "approve")

    results = await asyncio.wait_for(asyncio.gather(*waiters), timeout=2.0)
    assert all(r["status"] == "approved" for r in results)
    assert approval_id not in approval_module._resolution_waiters


# ----- Full UDS round-trip --------------------------------------------


@pytest.fixture()
def _short_sock():
    p = Path("/tmp") / f"approval-rpc-{uuid.uuid4().hex[:8]}.sock"
    yield p
    p.unlink(missing_ok=True)


@pytest.fixture()
async def rpc_server(_short_sock):
    srv = RuntimeRpcServer(str(_short_sock))
    approval_rpc_handlers.register(srv)
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


def _client_call_in_thread(socket_path, method, params=None, timeout=30.0):
    """Run RuntimeRpcClient.call in a worker thread so the event loop
    is free to drive the server side concurrently.
    """
    result_box: dict = {}
    err_box: list = []

    def _go():
        try:
            client = RuntimeRpcClient(socket_path)
            result_box["v"] = client.call(method, params, timeout=timeout)
        except Exception as exc:
            err_box.append(exc)

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    return t, result_box, err_box


async def _join_thread(t, *, timeout=5.0):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, t.join, timeout)
    if t.is_alive():
        raise AssertionError("RPC client thread hung")


@pytest.mark.asyncio
async def test_uds_create_then_wait_blocks_until_resolve(
    rpc_server, _empty_waiters,
):
    """Full path the MCP subprocess uses: open a socket, create the
    approval, then long-poll on approval.wait until the dashboard
    resolves it.
    """
    create_t, create_r, create_e = _client_call_in_thread(
        rpc_server._socket_path,
        "approval.create",
        {
            "agent_name": "agent_a",
            "title": "Deploy plan",
            "content": "Looks good?",
        },
    )
    await _join_thread(create_t, timeout=5.0)
    if create_e:
        raise create_e[0]
    approval_id = create_r["v"]["id"]

    # Long-poll. Pass timeout=None on the client so the socket blocks
    # rather than firing the default 30 s SO_RCVTIMEO.
    wait_t, wait_r, wait_e = _client_call_in_thread(
        rpc_server._socket_path,
        "approval.wait",
        {"approval_id": approval_id},
        timeout=None,
    )

    # Give the wait handler time to subscribe before we resolve.
    for _ in range(50):
        if approval_id in approval_module._resolution_waiters:
            break
        await asyncio.sleep(0.02)
    assert approval_id in approval_module._resolution_waiters

    approval_service.resolve_approval(approval_id, "approve", "ok")

    await _join_thread(wait_t, timeout=5.0)
    if wait_e:
        raise wait_e[0]
    result = wait_r["v"]
    assert result["user_decision"] == "approve"
    assert result["user_comment"] == "ok"


@pytest.mark.asyncio
async def test_uds_wait_returns_immediately_for_resolved(
    rpc_server, _empty_waiters,
):
    """Backend-restart recovery: if the client retries approval.wait
    after a socket drop and the approval already resolved during the
    gap, the handler must return the resolved record without blocking.
    """
    approval_id = _create_pending_approval()
    approval_service.resolve_approval(approval_id, "approve")

    wait_t, wait_r, wait_e = _client_call_in_thread(
        rpc_server._socket_path,
        "approval.wait",
        {"approval_id": approval_id},
    )
    await _join_thread(wait_t, timeout=5.0)
    if wait_e:
        raise wait_e[0]
    assert wait_r["v"]["user_decision"] == "approve"


@pytest.mark.asyncio
async def test_uds_wait_unknown_id_returns_error_envelope(
    rpc_server, _empty_waiters,
):
    wait_t, wait_r, wait_e = _client_call_in_thread(
        rpc_server._socket_path,
        "approval.wait",
        {"approval_id": "does-not-exist"},
    )
    await _join_thread(wait_t, timeout=5.0)
    if wait_e:
        raise wait_e[0]
    result = wait_r["v"]
    assert result.get("status") == 404
    assert "not found" in result.get("error", "")


# ─── Fail-loud team_name validation ────────────────────────────────


@pytest.fixture
def _seed_spawn_records():
    """Insert real spawn_records rows into the test DB. Rolled back by
    the surrounding mcp_db_isolation SAVEPOINT so rows don't leak.
    Uses the same SessionLocal as approval_service so team-resolution
    queries hit these inserts directly — no SQLAlchemy mocking gymnastics.
    """
    from core.database import SessionLocal, SpawnRecordModel
    import time as _time
    import uuid as _uuid

    inserted_run_ids: list[str] = []

    def _add(agent_name: str, team_name: str | None = None, status: str = "running"):
        run_id = _uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            db.add(SpawnRecordModel(
                run_id=run_id,
                agent_name=agent_name,
                team_name=team_name,
                status=status,
                started_at=_time.time(),
            ))
            db.commit()
            inserted_run_ids.append(run_id)
        finally:
            db.close()

    yield _add

    # Defensive cleanup (savepoint should handle, but pin it anyway).
    db = SessionLocal()
    try:
        if inserted_run_ids:
            db.query(SpawnRecordModel).filter(
                SpawnRecordModel.run_id.in_(inserted_run_ids)
            ).delete(synchronize_session=False)
            db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_create_rejects_when_supplied_team_name_disagrees_with_spawn_record(
    _seed_spawn_records,
):
    """Fail loud when LLM passes a team_name that doesn't match the
    agent's actual team in spawn_records. Prevents the bug where
    LLM-supplied workspace basename was paused as a fake agent and
    the real PM kept running.
    """
    _seed_spawn_records("Wren [PM]", team_name="tool-audit-approval-team")

    with pytest.raises(ValueError, match="does not match agent.*actual team"):
        approval_service.create_approval({
            "agent_name": "Wren [PM]",
            "team_name": "agile-team_9b4ec26b",   # workspace basename, NOT real team
            "title": "x",
            "content": "y",
        })


@pytest.mark.asyncio
async def test_create_accepts_when_team_name_matches(
    _seed_spawn_records,
):
    """Happy path — LLM supplies the correct team_name, request goes
    through and the authoritative pause list includes both members.
    """
    _seed_spawn_records("Wren [PM]", team_name="tool-audit-approval-team")
    _seed_spawn_records("Rowan [Dev]", team_name="tool-audit-approval-team")

    result = approval_service.create_approval({
        "agent_name": "Wren [PM]",
        "team_name": "tool-audit-approval-team",
        "title": "x",
        "content": "y",
    })

    assert result["team_name"] == "tool-audit-approval-team"
    assert set(result["paused_agents"]) == {"Wren [PM]", "Rowan [Dev]"}


@pytest.mark.asyncio
async def test_create_ignores_omitted_team_name_for_team_agent(
    _seed_spawn_records,
):
    """When LLM omits team_name entirely, the service still resolves
    the team from spawn_records — no fallback to solo. Otherwise an
    LLM that forgets the parameter would silently demote a team
    approval to a single-agent pause.
    """
    _seed_spawn_records("Wren [PM]", team_name="tool-audit-approval-team")
    _seed_spawn_records("Rowan [Dev]", team_name="tool-audit-approval-team")

    result = approval_service.create_approval({
        "agent_name": "Wren [PM]",
        "title": "x",
        "content": "y",
    })

    assert result["team_name"] == "tool-audit-approval-team"
    assert set(result["paused_agents"]) == {"Wren [PM]", "Rowan [Dev]"}


@pytest.mark.asyncio
async def test_create_solo_agent_no_spawn_record(
    _seed_spawn_records,
):
    """In-process Jarvis (no spawn_record) — solo pause is correct,
    not a fallback. Verified separately to pin the legitimate solo
    branch against the fail-loud branch above.
    """
    # No rows added — agent has no spawn_record.

    result = approval_service.create_approval({
        "agent_name": "Jarvis",
        "title": "x",
        "content": "y",
    })

    assert result["team_name"] is None
    assert result["paused_agents"] == ["Jarvis"]


# ─── HAPPY PATH e2e: approval → team pause → resolve → team resume ──


@pytest.mark.asyncio
async def test_approval_lifecycle_team_pauses_then_resumes_all_members(
    _seed_spawn_records,
):
    """HP-2a + HP-2b end-to-end: PM requests approval → ALL team members
    paused (verified via real pause_controller state). Resolve approval
    → ALL members resumed.

    This is the exact flow that shipped broken: approval_service was
    forwarding LLM-supplied team_name as scope to pause_controller, so
    when the LLM passed the workspace basename (≠ real team_name), the
    expansion failed and pause was a no-op. Without an end-to-end test
    on this path the regression was invisible.
    """
    from services.pause_controller import pause_controller

    _seed_spawn_records("Wren [PM]",   team_name="tool-audit-approval-team")
    _seed_spawn_records("Rowan [Dev]", team_name="tool-audit-approval-team")
    _seed_spawn_records("Sky [QE]",    team_name="tool-audit-approval-team")

    result = approval_service.create_approval({
        "agent_name": "Wren [PM]",
        "team_name": "tool-audit-approval-team",
        "title": "Deploy plan",
        "content": "Review please",
    })

    # ALL team members must actually be paused (not just stored as
    # paused_agents in the DB row).
    for member in ("Wren [PM]", "Rowan [Dev]", "Sky [QE]"):
        assert pause_controller.is_paused(member), \
            f"{member} should be paused after approval creation"

    # Resolve → ALL members must unpause.
    approval_service.resolve_approval(result["id"], "approve", "looks good")

    for member in ("Wren [PM]", "Rowan [Dev]", "Sky [QE]"):
        assert not pause_controller.is_paused(member), \
            f"{member} should be resumed after approval approve"


@pytest.mark.asyncio
async def test_approval_lifecycle_solo_agent_pauses_then_resumes(
    _seed_spawn_records,
):
    """HP for solo agent: in-process Jarvis (no spawn_record) requests
    approval → only Jarvis paused → resolve → Jarvis resumed.
    """
    from services.pause_controller import pause_controller

    # No spawn_record for Jarvis (in-process).

    result = approval_service.create_approval({
        "agent_name": "Jarvis",
        "title": "Solo plan",
        "content": "OK?",
    })

    assert pause_controller.is_paused("Jarvis")
    assert result["paused_agents"] == ["Jarvis"]

    approval_service.resolve_approval(result["id"], "reject")
    assert not pause_controller.is_paused("Jarvis")


@pytest.mark.asyncio
async def test_approval_lifecycle_omitted_team_name_still_pauses_team(
    _seed_spawn_records,
):
    """HP-2b: LLM forgets to pass team_name. The service must still
    resolve the team from the requester's spawn_record and pause the
    whole team — not silently demote to solo pause.

    Regression-pin for the failure mode where an LLM omitting a
    parameter would cause a team approval to behave like a solo one.
    """
    from services.pause_controller import pause_controller

    _seed_spawn_records("Wren [PM]",   team_name="tool-audit-approval-team")
    _seed_spawn_records("Rowan [Dev]", team_name="tool-audit-approval-team")

    result = approval_service.create_approval({
        "agent_name": "Wren [PM]",
        # team_name OMITTED — but spawn_records knows the truth
        "title": "Plan",
        "content": "OK?",
    })

    assert pause_controller.is_paused("Wren [PM]")
    assert pause_controller.is_paused("Rowan [Dev]"), \
        "omitting team_name must NOT demote a team approval to solo"
    assert result["team_name"] == "tool-audit-approval-team"


@pytest.mark.asyncio
async def test_approval_lifecycle_rejects_bogus_team_name(
    _seed_spawn_records,
):
    """HP-2 fail-loud: LLM passes a team_name that doesn't match the
    requester's spawn_record. Must raise ValueError so the MCP tool
    surfaces the error to the LLM. No silent fallback that pauses
    something arbitrary.
    """
    from services.pause_controller import pause_controller

    _seed_spawn_records("Wren [PM]", team_name="tool-audit-approval-team")

    with pytest.raises(ValueError, match="does not match"):
        approval_service.create_approval({
            "agent_name": "Wren [PM]",
            "team_name": "agile-team_garbage_session_id",  # LLM made this up
            "title": "x",
            "content": "y",
        })

    # CRITICAL: nothing should be paused on a rejected call. Pre-bug
    # behavior left a phantom pause row for the bogus team_name.
    assert not pause_controller.is_paused("Wren [PM]")
    assert not pause_controller.is_paused("agile-team_garbage_session_id")


# ─── HP: Manual resume guard while approval pending ───────────────


@pytest.mark.asyncio
async def test_manual_resume_blocked_while_approval_pending(
    _seed_spawn_records,
):
    """User clicks Resume on a team member while approval is pending →
    ``pause_controller.resume`` must raise ``PauseProtected``. Without
    this guard the controller would unpause the team while the
    approval is still pending and the subprocess is still blocked on
    ``approval.wait`` → state mismatch (controller=running, approval=
    pending, subprocess=blocked).
    """
    from services.pause_controller import pause_controller, PauseProtected

    _seed_spawn_records("Wren [PM]",   team_name="tool-audit-approval-team")
    _seed_spawn_records("Rowan [Dev]", team_name="tool-audit-approval-team")

    approval_service.create_approval({
        "agent_name": "Wren [PM]",
        "team_name": "tool-audit-approval-team",
        "title": "Plan",
        "content": "Review",
    })

    # Both members are paused. User tries to resume Wren manually.
    with pytest.raises(PauseProtected) as exc_info:
        pause_controller.resume("Wren [PM]")

    assert exc_info.value.agent_name == "Wren [PM]"
    assert exc_info.value.approval_id  # populated for UI deep-link

    # Nothing got resumed (whole-call atomicity — not "Rowan resumed
    # but Wren refused").
    assert pause_controller.is_paused("Wren [PM]")
    assert pause_controller.is_paused("Rowan [Dev]")


@pytest.mark.asyncio
async def test_resume_unblocks_after_approval_resolves(
    _seed_spawn_records,
):
    """Happy path: approval is rejected/approved → resume cascade runs
    → user can then resume manually (no-op since cascade did it, but
    must not raise PauseProtected).
    """
    from services.pause_controller import pause_controller

    _seed_spawn_records("Wren [PM]", team_name="tool-audit-approval-team")

    result = approval_service.create_approval({
        "agent_name": "Wren [PM]",
        "team_name": "tool-audit-approval-team",
        "title": "Plan",
        "content": "Review",
    })

    approval_service.resolve_approval(result["id"], "reject")

    # No pending approval now → guard must not fire.
    pause_controller.resume("Wren [PM]")  # idempotent no-op, must not raise
    assert not pause_controller.is_paused("Wren [PM]")


@pytest.mark.asyncio
async def test_multi_approval_one_resolved_still_blocks_resume(
    _seed_spawn_records,
):
    """Edge case: TWO concurrent pending approvals reference Wren.
    Resolving the first must NOT unpause Wren (the second still holds
    the lock). And user attempting manual resume must still see
    PauseProtected pointing at the second approval.
    """
    from services.pause_controller import pause_controller, PauseProtected

    _seed_spawn_records("Wren [PM]", team_name="team-x")

    a1 = approval_service.create_approval({
        "agent_name": "Wren [PM]",
        "team_name": "team-x",
        "title": "First",
        "content": "...",
    })
    a2 = approval_service.create_approval({
        "agent_name": "Wren [PM]",
        "team_name": "team-x",
        "title": "Second",
        "content": "...",
    })

    # Approving the first must NOT unpause Wren — second still holds it.
    approval_service.resolve_approval(a1["id"], "approve")
    assert pause_controller.is_paused("Wren [PM]"), \
        "agent held by another pending approval must stay paused"

    with pytest.raises(PauseProtected) as exc_info:
        pause_controller.resume("Wren [PM]")
    assert exc_info.value.approval_id == a2["id"], \
        "guard must point at the SECOND approval that's still pending"

    # Resolving the second finally releases.
    approval_service.resolve_approval(a2["id"], "approve")
    assert not pause_controller.is_paused("Wren [PM]")
