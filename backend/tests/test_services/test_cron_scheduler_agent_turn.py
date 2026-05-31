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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.cron_scheduler import ApprovalBlocked, CronScheduler


# All tests in this module exercise the *post-gate* behaviour of
# ``_execute_agent_turn``. The approval gate itself has its own
# coverage in test_approval_gate.py — here we patch it to auto-
# approve so each test can focus on the resume_and_send contract
# (originally added for the 2026-05-05 "no agent_name" regression).
# ``_execute_agent_turn`` imports the gate locally as
# ``from services.approval_gate import gate as _gate`` — patching
# at the source module ``services.approval_gate.gate`` is the only
# anchor that intercepts that local re-bind.
@pytest.fixture(autouse=True)
def _auto_approve_gate():
    async def _allow(**_kw):
        return True, "test auto-approve"
    with patch("services.approval_gate.gate", side_effect=_allow):
        yield


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
async def test_execute_agent_turn_raises_approval_blocked_when_gate_refuses(_auto_approve_gate):
    """When the approval gate returns ``approved=False`` the agent turn
    MUST raise :class:`ApprovalBlocked`. This is the contract _execute_job
    catches separately so a gate-blocked run is recorded as ``blocked``
    rather than ``success`` (PR #57 reviewer finding, comment on
    cron_scheduler.py:405).
    """
    # Replace the auto-approve fixture's gate with one that refuses.
    async def _refuse(**_kw):
        return False, "user rejected: not happy with payload"

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

    with patch("services.approval_gate.gate", side_effect=_refuse):
        with pytest.raises(ApprovalBlocked) as excinfo:
            await sched._execute_agent_turn(job)

    assert "user rejected" in str(excinfo.value)
    # The actual resume_and_send must NEVER be called when the gate refused.
    sched._session_service.resume_and_send.assert_not_called()
