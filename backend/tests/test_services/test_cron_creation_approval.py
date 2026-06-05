"""Creation-time approval for cron jobs — integration + e2e.

Design under test (replaces the old fire-time blocking gate):

* Approval is decided ONCE, at job CREATION, and stored on
  ``CronJobModel.approval_status`` — the single source of truth the scheduler
  reads at fire time. The scheduler NEVER blocks waiting for a human.
* Jobs an AGENT creates (``cron_create``) that run an agent turn start
  ``pending`` and get an ApprovalRequest card. Reminders / dashboard jobs are
  ``approved`` immediately.
* Resolving the card writes through to ``approval_status`` (approve → runs,
  reject → never runs).
* An agent editing the payload of an approved job resets it to ``pending``.
* Settings toggle ``scheduler.REQUIRE_APPROVAL=false`` bypasses the gate.

These run against the REAL DB (rolled back per-test via ``mcp_db_isolation``)
and the REAL ``create_approval`` / ``resolve_approval`` / ``_execute_agent_turn``
code. The only thing faked is the UDS RPC transport the MCP subprocess would
use to reach the backend — we route ``approval.create`` straight to
``approval_service.create_approval``, which is exactly what the production RPC
handler ``_approval_create`` does.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.database import (
    get_db_session, CronJobModel, ApprovalRequestModel, CronRunModel, NotificationModel,
)
from services.approval_service import approval_service
from services.cron_scheduler import ApprovalBlocked, CronScheduler


@pytest.fixture(autouse=True)
def _isolate_db(mcp_db_isolation):
    """All DB writes land in a SAVEPOINT rolled back on teardown."""
    yield


@pytest.fixture(autouse=True)
def _fake_rpc(monkeypatch):
    """Route the subprocess RPC ``approval.create`` to the in-process service,
    mirroring the real ``approval_rpc_handlers._approval_create`` forward. This
    lets ``cron_create`` build + ship its real approval-card params without a
    live Unix socket."""
    def _dispatch(method, params=None, **_kw):
        if method == "approval.create":
            return approval_service.create_approval(params or {})
        raise AssertionError(f"unexpected RPC method in test: {method}")

    monkeypatch.setattr("tools.runtime_rpc_client.call", _dispatch, raising=True)


def _only_job() -> CronJobModel:
    db = get_db_session()
    try:
        jobs = db.query(CronJobModel).all()
        assert len(jobs) == 1, f"expected exactly one job, got {len(jobs)}"
        return jobs[0]
    finally:
        db.close()


def _cron_approvals() -> list[ApprovalRequestModel]:
    db = get_db_session()
    try:
        return (
            db.query(ApprovalRequestModel)
            .filter(ApprovalRequestModel.approval_type == "cron_approval")
            .all()
        )
    finally:
        db.close()


# ── Creation-time gating ──────────────────────────────────────────────


def test_agent_agent_turn_job_starts_pending_with_card():
    from tools import cron_server

    out = cron_server.cron_create(
        name="Daily digest",
        cron_expr="*/5 * * * *",
        exec_mode="agent_turn",
        exec_payload="Summarize today's AI news",
        exec_agent="ResearchAgent",
    )
    assert "AWAITING YOUR APPROVAL" in out

    job = _only_job()
    assert job.created_by == "agent"
    assert job.approval_status == "pending", "agent-created agent_turn must start pending"

    cards = _cron_approvals()
    assert len(cards) == 1
    card = cards[0]
    assert card.status == "pending"
    assert json.loads(card.metadata_json)["job_id"] == job.id
    # pause=False: a deferred gate must NOT freeze the (possibly chatting) agent.
    assert json.loads(card.paused_agents or "[]") == []


def test_agent_reminder_job_is_approved_no_card():
    from tools import cron_server

    cron_server.cron_create(
        name="Take meds",
        cron_expr="0 8 * * *",
        exec_mode="reminder",
        exec_payload="Take your medicine",
    )
    job = _only_job()
    assert job.approval_status == "approved", "reminders carry no exec risk → no gate"
    assert _cron_approvals() == []


def test_toggle_off_bypasses_creation_gate(monkeypatch):
    """scheduler.REQUIRE_APPROVAL=false → agent_turn job is created already
    approved with no card (use at your own risk)."""
    from tools import cron_server

    monkeypatch.setattr(
        "services.config_service.config_service.get",
        lambda category, key, default=None: "false"
        if (category, key) == ("scheduler", "REQUIRE_APPROVAL") else default,
    )
    cron_server.cron_create(
        name="No-gate job",
        cron_expr="*/5 * * * *",
        exec_mode="agent_turn",
        exec_payload="do thing",
        exec_agent="Jarvis",
    )
    job = _only_job()
    assert job.approval_status == "approved"
    assert _cron_approvals() == []


# ── Resolve write-through ──────────────────────────────────────────────


def test_approve_flips_job_to_approved():
    from tools import cron_server

    cron_server.cron_create(
        name="Approve me", cron_expr="*/5 * * * *", exec_mode="agent_turn",
        exec_payload="run report", exec_agent="Jarvis",
    )
    job_id = _only_job().id
    card = _cron_approvals()[0]

    approval_service.resolve_approval(card.id, decision="approve")

    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        assert job.approval_status == "approved"
    finally:
        db.close()


def test_reject_marks_job_rejected():
    from tools import cron_server

    cron_server.cron_create(
        name="Reject me", cron_expr="*/5 * * * *", exec_mode="agent_turn",
        exec_payload="rm -rf /", exec_agent="Jarvis",
    )
    job_id = _only_job().id
    card = _cron_approvals()[0]

    approval_service.resolve_approval(card.id, decision="reject", comment="nope")

    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        assert job.approval_status == "rejected"
    finally:
        db.close()


# ── Edit re-gates ──────────────────────────────────────────────────────


def test_agent_payload_edit_resets_to_pending():
    from tools import cron_server

    cron_server.cron_create(
        name="Editable", cron_expr="*/5 * * * *", exec_mode="agent_turn",
        exec_payload="original", exec_agent="Jarvis",
    )
    job_id = _only_job().id
    approval_service.resolve_approval(_cron_approvals()[0].id, decision="approve")

    # Agent swaps the payload after approval — must re-gate.
    cron_server.cron_update(job_id=job_id, exec_payload="MALICIOUS new payload")

    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        assert job.approval_status == "pending", "edited payload must reset approval"
    finally:
        db.close()
    # A fresh card for the new payload exists (pending).
    pending = [c for c in _cron_approvals() if c.status == "pending"]
    assert len(pending) == 1


def test_agent_no_op_edit_keeps_approval():
    """Re-sending the SAME payload is not a change → no reset."""
    from tools import cron_server

    cron_server.cron_create(
        name="Same", cron_expr="*/5 * * * *", exec_mode="agent_turn",
        exec_payload="same payload", exec_agent="Jarvis",
    )
    job_id = _only_job().id
    approval_service.resolve_approval(_cron_approvals()[0].id, decision="approve")

    cron_server.cron_update(job_id=job_id, exec_payload="same payload")

    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        assert job.approval_status == "approved"
    finally:
        db.close()


def test_agent_swapping_exec_agent_resets_to_pending():
    """exec_agent is a vetted artifact (the card shows the target agent). An
    agent that swaps a WEAK approved agent for a POWERFUL one — same payload —
    must re-gate; otherwise the vetted toolset is bypassed."""
    from tools import cron_server

    cron_server.cron_create(
        name="Agent swap", cron_expr="*/5 * * * *", exec_mode="agent_turn",
        exec_payload="do it", exec_agent="WeakAgent",
    )
    job_id = _only_job().id
    approval_service.resolve_approval(_cron_approvals()[0].id, decision="approve")

    cron_server.cron_update(job_id=job_id, exec_agent="PowerfulAgent")

    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        assert job.approval_status == "pending", "swapping exec_agent must re-gate"
    finally:
        db.close()
    assert len([c for c in _cron_approvals() if c.status == "pending"]) == 1


def test_agent_changing_schedule_resets_to_pending():
    """schedule_cron is a vetted artifact (the card shows the schedule). An
    agent that changes WHEN / how often an approved payload fires must re-gate."""
    from tools import cron_server

    cron_server.cron_create(
        name="Reschedule", cron_expr="0 9 * * *", exec_mode="agent_turn",
        exec_payload="do it", exec_agent="Jarvis",
    )
    job_id = _only_job().id
    approval_service.resolve_approval(_cron_approvals()[0].id, decision="approve")

    cron_server.cron_update(job_id=job_id, cron_expr="*/1 * * * *")

    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        assert job.approval_status == "pending", "changing the schedule must re-gate"
    finally:
        db.close()
    assert len([c for c in _cron_approvals() if c.status == "pending"]) == 1


# ── Dashboard edit supersedes a pending agent approval ────────────────


def test_dashboard_edit_approves_pending_and_clears_card():
    """A trusted dashboard edit of an agent-created pending job marks it
    approved and resolves the stale card — so a later click on the old card
    can't approve a payload the user never saw."""
    from tools import cron_server
    from routes.scheduler import _approve_pending_cron_cards
    from core.database import ApprovalRequestModel

    cron_server.cron_create(
        name="Dash edit", cron_expr="*/5 * * * *", exec_mode="agent_turn",
        exec_payload="agent payload", exec_agent="Jarvis",
    )
    job_id = _only_job().id
    assert _only_job().approval_status == "pending"

    # Simulate the dashboard PUT having flipped the flag, then clearing cards.
    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        job.approval_status = "approved"
        db.commit()
    finally:
        db.close()
    _approve_pending_cron_cards(job_id)

    assert _only_job().approval_status == "approved"
    # No pending cron card remains.
    db = get_db_session()
    try:
        pending = (
            db.query(ApprovalRequestModel)
            .filter(
                ApprovalRequestModel.approval_type == "cron_approval",
                ApprovalRequestModel.status == "pending",
            )
            .all()
        )
        assert pending == []
    finally:
        db.close()


# ── End-to-end: creation → approval → fire ────────────────────────────


@pytest.mark.asyncio
async def test_e2e_pending_job_skipped_then_runs_after_approval():
    """Full flow: agent creates job → fire is SKIPPED while pending → user
    approves → next fire RUNS the real resume_and_send."""
    from tools import cron_server

    cron_server.cron_create(
        name="E2E", cron_expr="*/5 * * * *", exec_mode="agent_turn",
        exec_payload="do the thing", exec_agent="Jarvis",
    )
    job_id = _only_job().id

    sched = CronScheduler()
    sched._agent_app = MagicMock()
    sched._session_service = MagicMock()
    sched._session_service.resume_and_send = AsyncMock(return_value=("done", "sid"))

    def _fresh_job():
        db = get_db_session()
        try:
            return db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        finally:
            db.close()

    # 1) Pending → fire is skipped (ApprovalBlocked, no inbox spam, no run).
    with pytest.raises(ApprovalBlocked) as ex:
        await sched._execute_agent_turn(_fresh_job())
    assert ex.value.notify is False
    sched._session_service.resume_and_send.assert_not_called()

    # 2) User approves.
    approval_service.resolve_approval(_cron_approvals()[0].id, decision="approve")

    # 3) Next fire runs for real.
    result = await sched._execute_agent_turn(_fresh_job())
    assert result == "done"
    sched._session_service.resume_and_send.assert_called_once()


@pytest.mark.asyncio
async def test_pending_job_skips_silently_then_runs_after_approval():
    """Through the REAL scheduler entry point ``_execute_job`` (what the loop
    calls), not just ``_execute_agent_turn``:

    * a pending job → skipped SILENTLY: no run row, no inbox notification
      (the approval card already notified), no fail-count / run-count bump,
      job stays ``active`` for the next tick. Ticking repeatedly must not
      accrue rows (unbounded-growth guard).
    * after approval → the same path RUNS, records ``success`` + one
      ``agent_result`` notification.

    Only the LLM seam (``resume_and_send``) is mocked.
    """
    from tools import cron_server

    cron_server.cron_create(
        name="Sched path", cron_expr="*/5 * * * *", exec_mode="agent_turn",
        exec_payload="do it", exec_agent="Jarvis",
    )
    job_id = _only_job().id

    sched = CronScheduler()
    sched._agent_app = MagicMock()
    sched._session_service = MagicMock()
    sched._session_service.resume_and_send = AsyncMock(return_value=("done", "sid"))

    # 1) Pending → _execute_job catches ApprovalBlocked and records NOTHING:
    #    no run row, no notification. Tick it THREE times to prove an
    #    unapproved recurring job does not accrue blocked rows per tick (the
    #    ~288-rows/day unbounded-growth regression).
    for _ in range(3):
        db = get_db_session()
        try:
            await sched._execute_job(
                db.query(CronJobModel).filter(CronJobModel.id == job_id).first(), db
            )
        finally:
            db.close()

    db = get_db_session()
    try:
        runs = db.query(CronRunModel).filter(CronRunModel.job_id == job_id).all()
        assert runs == [], (
            "awaiting-approval skip must NOT persist a run row — a never-approved "
            f"recurring job would grow unbounded; saw {len(runs)} rows after 3 ticks"
        )
        notifs = db.query(NotificationModel).filter(NotificationModel.job_id == job_id).all()
        assert notifs == [], "pending-approval skip must NOT create an inbox notification"
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        assert job.approval_status == "pending"
        assert job.status == "active"
        assert (job.fail_count or 0) == 0
        assert (job.run_count or 0) == 0, "silent skip must not inflate run_count"
    finally:
        db.close()
    sched._session_service.resume_and_send.assert_not_called()

    # 2) Approve → next _execute_job runs for real.
    approval_service.resolve_approval(_cron_approvals()[0].id, decision="approve")
    db = get_db_session()
    try:
        await sched._execute_job(
            db.query(CronJobModel).filter(CronJobModel.id == job_id).first(), db
        )
    finally:
        db.close()

    sched._session_service.resume_and_send.assert_called_once()
    db = get_db_session()
    try:
        runs = db.query(CronRunModel).filter(CronRunModel.job_id == job_id).order_by(CronRunModel.id).all()
        assert runs[-1].status == "success"
        notifs = db.query(NotificationModel).filter(NotificationModel.job_id == job_id).all()
        assert len(notifs) == 1, "successful agent run records one agent_result notification"
    finally:
        db.close()
