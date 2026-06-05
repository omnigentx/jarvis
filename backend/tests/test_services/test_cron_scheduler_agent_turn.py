"""CronScheduler.``_execute_agent_turn`` regression tests.

Production bug found 2026-05-05: a scheduled "Daily AI news digest"
job configured with ``exec_agent="ResearchAgent"`` ran for 45 min
and was marked SUCCESS but the notification body was "No response". The
UI showed ``ResearchAgent`` as the agent because that string is read
straight from ``job.exec_agent`` metadata — but the cron code never
actually passed ``agent_name`` into ``resume_and_send``. The default
fallback inside session_service routes to Jarvis instead.

This test fences that contract: whatever ``exec_agent`` the user
configures on the cron job MUST be the ``agent_name`` argument to
``resume_and_send``. A diff that drops or renames that kwarg will fail
this test.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.cron_scheduler import ApprovalBlocked, CronScheduler


# All tests in this module exercise the *post-approval* behaviour of
# ``_execute_agent_turn``. Approval is now decided at CREATION time and
# stored on ``job.approval_status`` (single source of truth) — the run path
# only READS it (no blocking gate any more). So "auto-approve" here simply
# means setting ``job.approval_status = "approved"`` on the stub job. The
# creation-time gating + resolve flow has its own coverage in
# test_cron_creation_approval.py.


@pytest.mark.asyncio
async def test_execute_agent_turn_passes_configured_agent_name():
    sched = CronScheduler()
    sched._agent_app = MagicMock()
    sched._session_service = MagicMock()

    captured_kwargs: dict = {}

    async def _fake_resume(_app, _payload, **kwargs):
        captured_kwargs.update(kwargs)
        return "summary text", "session-x"
    sched._session_service.resume_and_send = _fake_resume

    job = MagicMock()
    job.id = 7
    job.name = "Daily AI digest"
    job.exec_agent = "ResearchAgent"
    job.exec_payload = "Tổng hợp tin tức AI"
    job.approval_status = "approved"  # vetted at creation time

    result = await sched._execute_agent_turn(job)

    assert result == "summary text"
    assert captured_kwargs.get("agent_name") == "ResearchAgent", (
        f"resume_and_send was called without (or with wrong) agent_name; "
        f"actual kwargs: {captured_kwargs}. Without this kwarg the call "
        "falls back to the default Jarvis agent and the configured target "
        "(e.g. ResearchAgent) never runs — the bug that produced "
        "'success / 45 min / No response' notifications."
    )


@pytest.mark.asyncio
async def test_execute_agent_turn_logs_warning_on_empty_response(caplog):
    """Empty response from a SUCCESS-marked turn must be loud-logged so
    the next investigation has evidence without re-running the job.
    """
    import logging
    sched = CronScheduler()
    sched._agent_app = MagicMock()
    sched._session_service = MagicMock()

    async def _fake_resume(_app, _payload, **kwargs):
        return "", "session-x"
    sched._session_service.resume_and_send = _fake_resume

    job = MagicMock()
    job.id = 8
    job.name = "Empty response job"
    job.exec_agent = "ResearchAgent"
    job.exec_payload = "do something"
    job.approval_status = "approved"

    with caplog.at_level(logging.WARNING, logger="services.cron_scheduler"):
        result = await sched._execute_agent_turn(job)

    assert result == "No response"
    assert any(
        "EMPTY response" in r.message and "ResearchAgent" in r.message
        for r in caplog.records
    ), f"empty-response warning missing; records: {[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_get_next_due_excludes_in_flight_jobs(monkeypatch):
    """Reviewer HIGH regression: after dispatch the loop must not re-pick
    the in-flight job. ``_get_next_due`` filters by ``_inflight_tasks`` so
    even with the spawn race window (status still ``active`` between
    create_task and the task actually running), the loop sees None / next
    job instead and avoids the 100%-CPU spin.
    """
    sched = CronScheduler()

    # Two stub job rows — both active, both overdue, simulating the moment
    # right after start() dispatched job "A" and is about to re-evaluate.
    class _Row:
        def __init__(self, jid):
            self.id = jid
            self.status = "active"
            self.next_run_at = 1.0  # any past timestamp
            self.name = f"job-{jid}"

    rows = [_Row("A"), _Row("B")]

    class _FakeQuery:
        def __init__(self):
            self._filtered = list(rows)

        def filter(self, *clauses):
            # Inspect the clause for the ~CronJobModel.id.in_(...) we added.
            for clause in clauses:
                txt = str(clause)
                # Pick up the "id NOT IN (...)" produced by ~id.in_(inflight_ids)
                if "id NOT IN" in txt or "NOT (cron_jobs.id IN" in txt:
                    # Parameters live on the BindParameter — extract the inflight set
                    # via the compiled clause for the test. Simpler: just exclude "A".
                    self._filtered = [r for r in self._filtered if r.id != "A"]
            return self

        def order_by(self, *a):
            return self

        def first(self):
            return self._filtered[0] if self._filtered else None

    class _FakeDb:
        def query(self, *a):
            return _FakeQuery()

        def expunge(self, _):
            pass

        def close(self):
            pass

    monkeypatch.setattr("services.cron_scheduler.get_db_session", lambda: _FakeDb())

    # Mark job "A" as in-flight via a never-resolving sentinel task.
    async def _hang():
        await asyncio.sleep(60)
    sched._inflight_tasks["A"] = asyncio.create_task(_hang())
    try:
        # Now _get_next_due should pick "B", not "A", even though "A" is in
        # the underlying row list with an earlier next_run_at.
        next_job, _sleep = sched._get_next_due()
        assert next_job is not None
        assert next_job.id == "B"
    finally:
        sched._inflight_tasks["A"].cancel()


@pytest.mark.asyncio
async def test_run_job_isolated_uses_fresh_db_session(monkeypatch):
    """``_run_job_isolated`` must open + close its own DB session so a long-
    running task (eg awaiting human approval) doesn't pin the scheduler-
    loop's session for an hour. Verifies the basic plumbing: a session is
    obtained, the job is looked up, ``_execute_job`` is called, the
    session is closed in finally."""
    sched = CronScheduler()

    closed_sessions: list[str] = []

    class _Row:
        id = "abc"
        status = "active"
        name = "x"

    class _Db:
        def __init__(self, label):
            self.label = label

        def query(self, *a):
            class _Q:
                def filter(self, *a):
                    return self

                def first(_self):
                    return _Row()
            return _Q()

        def close(self):
            closed_sessions.append(self.label)

    counter = {"n": 0}

    def _new_session():
        counter["n"] += 1
        return _Db(f"sess-{counter['n']}")

    monkeypatch.setattr("services.cron_scheduler.get_db_session", _new_session)

    exec_calls: list[str] = []

    async def _fake_execute(self, job, db):
        exec_calls.append(getattr(db, "label", "?"))
    monkeypatch.setattr(CronScheduler, "_execute_job", _fake_execute)

    await sched._run_job_isolated("abc")
    assert exec_calls == ["sess-1"]
    assert closed_sessions == ["sess-1"]


@pytest.mark.asyncio
async def test_execute_agent_turn_raises_approval_blocked_when_not_approved():
    """A not-yet-approved job (``approval_status='pending'``) MUST raise
    :class:`ApprovalBlocked` at fire time — NEVER block waiting for a human,
    and NEVER run the payload. _execute_job catches this and records the run
    as ``blocked`` (not ``success``). This is the creation-time-approval
    replacement for the old fire-time blocking gate.
    """
    sched = CronScheduler()
    sched._agent_app = MagicMock()
    sched._session_service = MagicMock()
    sched._session_service.resume_and_send = AsyncMock(return_value=("should not run", "sid"))

    job = MagicMock()
    job.id = "abc123"
    job.name = "Blocked job"
    job.exec_agent = "Jarvis"
    job.exec_payload = "do thing"
    job.schedule_cron = "* * * * *"
    job.calendar_type = "solar"
    job.approval_status = "pending"  # agent-created, not yet vetted

    # Default config (no scheduler.REQUIRE_APPROVAL row) → approval required.
    with pytest.raises(ApprovalBlocked) as excinfo:
        await sched._execute_agent_turn(job)

    assert excinfo.value.notify is False, "pending-approval skips must not spam the inbox"
    assert "approval" in str(excinfo.value).lower()
    # The actual resume_and_send must NEVER be called for an unapproved job.
    sched._session_service.resume_and_send.assert_not_called()


@pytest.mark.asyncio
async def test_execute_agent_turn_runs_when_approval_disabled(monkeypatch):
    """With the Settings toggle scheduler.REQUIRE_APPROVAL=false, an
    otherwise-pending job runs immediately — the documented "use at your own
    risk" bypass."""
    sched = CronScheduler()
    sched._agent_app = MagicMock()
    sched._session_service = MagicMock()
    sched._session_service.resume_and_send = AsyncMock(return_value=("ran", "sid"))

    job = MagicMock()
    job.id = "bypass1"
    job.name = "Bypass job"
    job.exec_agent = "Jarvis"
    job.exec_payload = "do thing"
    job.approval_status = "pending"

    monkeypatch.setattr(
        "services.config_service.config_service.get",
        lambda category, key, default=None: "false"
        if (category, key) == ("scheduler", "REQUIRE_APPROVAL") else default,
    )

    result = await sched._execute_agent_turn(job)
    assert result == "ran"
    sched._session_service.resume_and_send.assert_called_once()
