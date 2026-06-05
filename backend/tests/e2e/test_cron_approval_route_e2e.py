"""E2E: the REAL Approvals HTTP route makes an agent-created cron job runnable.

This drives the exact endpoint the frontend's "Approve" button calls —
``PUT /api/approvals/{id}/resolve`` — through production FastAPI routing,
auth, the ``approval_service.resolve_approval`` resolve hook, and back onto
``CronJobModel.approval_status``. Real SQLite; nothing in the path is mocked.

It closes the gap left by the service-level integration tests, which call
``resolve_approval`` directly and never exercise the HTTP layer the user
actually hits.

The MCP subprocess → UDS RPC → ``approval.create`` transport is proven
separately in ``test_services/test_approval_rpc.py``; here we seed the same
row that path produces and focus on the resolve-via-HTTP click-flow.
"""
from __future__ import annotations

import pytest

from core.database import (
    get_db_session, CronJobModel, ApprovalRequestModel, CronRunModel,
)
from services.approval_service import approval_service

JOB_ID = "e2eaprjob"


def _cleanup():
    db = get_db_session()
    try:
        db.query(CronRunModel).filter(CronRunModel.job_id == JOB_ID).delete()
        db.query(CronJobModel).filter(CronJobModel.id == JOB_ID).delete()
        db.query(ApprovalRequestModel).filter(
            ApprovalRequestModel.approval_type == "cron_approval"
        ).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _open_setup_gate(monkeypatch):
    """The setup-gate middleware returns 503 on a fresh DB until the wizard is
    complete. Force it open so we exercise the approvals route, not the gate."""
    from middleware import setup_gate
    monkeypatch.setattr(setup_gate, "is_setup_complete", lambda: True)


@pytest.fixture()
def _seeded_pending_job():
    """An agent-created agent_turn job in 'pending' + its approval card —
    the exact state cron_create produces in production."""
    # Service tests that ran earlier use a SAVEPOINT fixture that closes its
    # engine connection at teardown, which can leave a stale connection in the
    # shared SQLite pool. Dispose it so this engine-backed e2e starts clean
    # regardless of test order.
    from core.database import engine
    engine.dispose()
    _cleanup()
    db = get_db_session()
    try:
        db.add(CronJobModel(
            id=JOB_ID, user_id="default", name="E2E approve me",
            schedule_cron="*/5 * * * *", calendar_type="solar",
            exec_mode="agent_turn", exec_payload="run report", exec_agent="Jarvis",
            status="active", created_by="agent", approval_status="pending",
        ))
        db.commit()
    finally:
        db.close()
    card = approval_service.create_approval({
        "agent_name": "Jarvis",
        "approval_type": "cron_approval",
        "title": "Schedule approval: E2E approve me",
        "content": "## payload\n\nrun report",
        "content_format": "markdown",
        "pause": False,
        "metadata": {"job_id": JOB_ID},
    })
    yield card
    _cleanup()


@pytest.mark.asyncio
async def test_resolve_route_approve_makes_job_runnable(app_client, _seeded_pending_job):
    card_id = _seeded_pending_job["id"]

    resp = await app_client.put(
        f"/api/approvals/{card_id}/resolve", json={"decision": "approve"}
    )
    assert resp.status_code == 200, resp.text

    # The job the scheduler reads at fire time is now approved.
    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == JOB_ID).first()
        assert job.approval_status == "approved"
    finally:
        db.close()

    # And the dashboard API surfaces it (the badge source).
    jobs = (await app_client.get("/api/scheduler/jobs")).json()["jobs"]
    mine = next(j for j in jobs if j["id"] == JOB_ID)
    assert mine["approval_status"] == "approved"


@pytest.mark.asyncio
async def test_resolve_route_reject_blocks_job(app_client, _seeded_pending_job):
    card_id = _seeded_pending_job["id"]

    resp = await app_client.put(
        f"/api/approvals/{card_id}/resolve",
        json={"decision": "reject", "comment": "did not ask for this"},
    )
    assert resp.status_code == 200, resp.text

    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == JOB_ID).first()
        assert job.approval_status == "rejected"
    finally:
        db.close()


def _seed_job(approval_status: str):
    """Seed an agent-created agent_turn job at a given approval_status.
    Disposes the shared engine first (see _seeded_pending_job for why)."""
    from core.database import engine
    engine.dispose()
    _cleanup()
    db = get_db_session()
    try:
        db.add(CronJobModel(
            id=JOB_ID, user_id="default", name="Edit me",
            schedule_cron="*/5 * * * *", calendar_type="solar",
            exec_mode="agent_turn", exec_payload="run report", exec_agent="Jarvis",
            status="active", created_by="agent", approval_status=approval_status,
        ))
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_dashboard_edit_of_pending_job_approves_via_route(app_client):
    """The REAL PUT /api/scheduler/jobs/{id} — a trusted dashboard edit of a
    PENDING job is the vetting step → it becomes approved."""
    _seed_job("pending")
    try:
        resp = await app_client.put(
            f"/api/scheduler/jobs/{JOB_ID}", json={"name": "Renamed by user"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["job"]["approval_status"] == "approved"
    finally:
        _cleanup()


@pytest.mark.asyncio
async def test_dashboard_edit_of_rejected_job_stays_rejected_via_route(app_client):
    """A REJECTED job is a deliberate 'no'. An incidental dashboard edit (a
    rename) must NOT silently revive it — approval_status stays rejected."""
    _seed_job("rejected")
    try:
        resp = await app_client.put(
            f"/api/scheduler/jobs/{JOB_ID}", json={"name": "Sneaky rename"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["job"]["approval_status"] == "rejected", (
            "a trivial edit must not auto-approve a job the user explicitly rejected"
        )
    finally:
        _cleanup()
