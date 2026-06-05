"""Regression tests for the always-on token-persistence hook.

Production bug found 2026-05-15: scheduler-triggered agent_turn calls
were silently NOT counted in the ``token_usage`` table. ``9router``
showed 16 requests / 864K input tokens; the Jarvis dashboard showed
3 calls / 78.8K — exactly the chat+voice+inject turns, never the cron
ones.

Root cause: token persistence used to live inside ``create_progress_hooks``
which only chat / voice / inject routes attached. ``cron_scheduler``
called ``session_service.resume_and_send`` directly without attaching
those hooks, so every LLM call its scheduled jobs made bypassed the
``token_usage`` insert path entirely.

Fix: centralize token persistence as an always-on hook attached at
app startup to every in-process agent. Callers set the
``current_run_id`` ContextVar to tag rows with their request id; the
hook reads the var at LLM-call time. This file pins:

  1. The factory builds a hook that calls ``_persist_and_broadcast_token_usage``
     using the run_id from ContextVar.
  2. ``cron_scheduler._execute_agent_turn`` sets that ContextVar so the
     hook sees a cron-shaped run_id during the agent's send call.
  3. End-to-end: simulating an LLM call with the hook attached writes
     exactly one ``token_usage`` row tagged with the caller's run_id.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.sse_progress import (
    current_run_id,
    create_token_persistence_hooks,
    attach_token_persistence_hooks_to_all,
)


# Cron agent-turn tests below call ``_execute_agent_turn`` directly. Since
# the approval-gate fix landed in cron_scheduler, those calls would
# otherwise reach into the real approval pipeline + DB and wait the full
# 1h gate timeout in CI. Patch the gate at the source module so the local
# `from services.approval_gate import gate as _gate` inside
# `_execute_agent_turn` sees the auto-approving stub.
@pytest.fixture(autouse=True)
def _auto_approve_cron_gate():
    async def _allow(**_kw):
        return True, "test auto-approve"
    with patch("services.approval_gate.gate", side_effect=_allow):
        yield


def _fake_agent_with_usage(input_tokens=100, output_tokens=20, model="gpt-5.5"):
    """Build a minimal stand-in for a fast-agent ``LlmAgent`` with a
    usage_accumulator that the hook can read like the real thing."""
    cache = SimpleNamespace(cache_hit_tokens=0, cache_read_tokens=0, cache_write_tokens=0)
    last_turn = SimpleNamespace(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cache_usage=cache,
        reasoning_tokens=0,
    )
    accumulator = SimpleNamespace(turns=[last_turn])
    return SimpleNamespace(name="Jarvis", usage_accumulator=accumulator)


@pytest.mark.asyncio
async def test_token_hook_reads_run_id_from_context_var():
    """The factory builds a hook that reads ``current_run_id`` at LLM-call
    time — proving the always-on hook can be attached once at startup and
    still tag each row with the caller's own run_id."""
    hooks = create_token_persistence_hooks()
    assert hooks.after_llm_call is not None

    captured = {}

    def _capture(agent_name, run_id, tokens):
        captured["agent_name"] = agent_name
        captured["run_id"] = run_id
        captured["tokens"] = tokens

    with patch("services.sse_progress._persist_and_broadcast_token_usage", side_effect=_capture):
        runner = SimpleNamespace(_agent=_fake_agent_with_usage())
        token = current_run_id.set("chat-abc12345")
        try:
            await hooks.after_llm_call(runner, MagicMock())
        finally:
            current_run_id.reset(token)

    assert captured["run_id"] == "chat-abc12345", (
        f"hook did not propagate ContextVar run_id; got {captured.get('run_id')!r}"
    )
    assert captured["tokens"]["input"] == 100
    assert captured["tokens"]["output"] == 20


@pytest.mark.asyncio
async def test_token_hook_uses_empty_run_id_when_context_var_not_set():
    """If no caller set the ContextVar (legacy / direct internal call),
    the hook still writes a row with empty run_id — better than silently
    losing the LLM call."""
    hooks = create_token_persistence_hooks()
    captured = {}

    def _capture(agent_name, run_id, tokens):
        captured["run_id"] = run_id

    with patch("services.sse_progress._persist_and_broadcast_token_usage", side_effect=_capture):
        runner = SimpleNamespace(_agent=_fake_agent_with_usage())
        # Do NOT set the ContextVar — simulate a caller that bypassed
        # the run_id ceremony. We still want the row written.
        await hooks.after_llm_call(runner, MagicMock())

    assert captured["run_id"] == "", (
        f"expected empty run_id when ContextVar unset; got {captured.get('run_id')!r}"
    )


def test_attach_to_all_is_idempotent_and_merges_with_existing_hooks():
    """Attaching twice (e.g. after dynamic-agent reload) must NOT stack
    duplicate hooks — that would double-count every LLM call."""
    from fast_agent.agents.tool_runner import ToolRunnerHooks

    # Agent without any prior hooks — fresh attach
    agent_a = SimpleNamespace(name="A", tool_runner_hooks=None)

    # Agent with a pre-existing (non-token) hook — must be preserved
    pre_existing_called = []

    async def _pre_hook(runner, msg):
        pre_existing_called.append(True)

    agent_b = SimpleNamespace(
        name="B",
        tool_runner_hooks=ToolRunnerHooks(after_llm_call=_pre_hook),
    )

    fake_app = SimpleNamespace(_agents={"A": agent_a, "B": agent_b})

    n1 = attach_token_persistence_hooks_to_all(fake_app)
    assert n1 == 2, f"first attach should hook both agents; got {n1}"
    assert agent_a.tool_runner_hooks is not None
    assert agent_b.tool_runner_hooks is not None
    assert getattr(agent_a, "_jarvis_token_hook") is True
    assert getattr(agent_b, "_jarvis_token_hook") is True

    # Re-attach (simulates dynamic_agents.db_rev_poll_loop firing a
    # second time after a rev bump). Must be a no-op per agent —
    # idempotency guard.
    n2 = attach_token_persistence_hooks_to_all(fake_app)
    assert n2 == 0, f"second attach must be no-op; got {n2}"


@pytest.mark.asyncio
async def test_cron_scheduler_sets_context_var_during_agent_turn():
    """``_execute_agent_turn`` must set the ContextVar so the always-on
    hook sees a cron-shaped run_id when the agent makes LLM calls.

    This is the *integration contract* between cron_scheduler and the
    centralized token-persistence hook. Without this kwarg the original
    bug returns: every cron LLM call writes an un-tagged row, and the
    dashboard's per-conversation breakdown is meaningless for scheduled
    jobs."""
    from services.cron_scheduler import CronScheduler

    sched = CronScheduler()
    sched._agent_app = MagicMock()
    sched._session_service = MagicMock()

    seen_during_call: dict = {}

    async def _fake_resume(_app, _payload, **kwargs):
        # The whole point: inside the agent send we should see the
        # cron-shaped run_id, because the always-on hook will read this
        # at the after_llm_call event.
        seen_during_call["run_id"] = current_run_id.get()
        return "ok", "sess-x"

    sched._session_service.resume_and_send = _fake_resume

    job = MagicMock()
    job.id = 42
    job.name = "test"
    job.exec_agent = "Jarvis"
    job.exec_payload = "hello"
    # Approved at creation time — this test exercises the token-context
    # contract downstream of the creation-time approval gate, not the gate
    # itself (a bare MagicMock's approval_status would block the turn).
    job.approval_status = "approved"

    # Pre-condition: outside the call, ContextVar is its default empty.
    assert current_run_id.get() == ""

    await sched._execute_agent_turn(job)

    assert seen_during_call.get("run_id", "").startswith("cron-42-"), (
        f"cron did not stamp run_id; saw {seen_during_call.get('run_id')!r}. "
        "Without this stamping the always-on token hook writes rows with "
        "empty run_id and the dashboard loses cron-job correlation."
    )

    # Post-condition: ContextVar is reset after the call returns, so a
    # concurrent task doesn't accidentally inherit the cron run_id.
    assert current_run_id.get() == "", (
        "ContextVar leaked past _execute_agent_turn — concurrent in-process "
        "calls would now mis-tag their token rows with this cron's run_id"
    )


@pytest.mark.asyncio
async def test_cron_scheduler_resets_context_var_on_exception():
    """If the agent send raises, the ContextVar must still reset.
    Otherwise the next task picked off the asyncio loop would inherit
    the failed cron's run_id and mis-tag its tokens."""
    from services.cron_scheduler import CronScheduler

    sched = CronScheduler()
    sched._agent_app = MagicMock()
    sched._session_service = MagicMock()

    async def _fake_resume_boom(_app, _payload, **kwargs):
        raise RuntimeError("provider blew up")

    sched._session_service.resume_and_send = _fake_resume_boom

    job = MagicMock()
    job.id = 99
    job.name = "boom job"
    job.exec_agent = "Jarvis"
    job.exec_payload = "x"
    # Approved so the turn proceeds to the agent send (where the boom is
    # raised) rather than being short-circuited by the approval gate.
    job.approval_status = "approved"

    with pytest.raises(RuntimeError, match="Agent turn failed"):
        await sched._execute_agent_turn(job)

    assert current_run_id.get() == "", (
        "ContextVar leaked after exception path — failure should NEVER "
        "leave the var dirty for the next task in the loop"
    )


@pytest.mark.asyncio
async def test_end_to_end_hook_writes_row_to_token_usage_table(tmp_path, monkeypatch):
    """End-to-end: with the hook attached, firing an after_llm_call event
    writes exactly one row to the SQLite ``token_usage`` table tagged
    with the ContextVar's run_id. This pins the full path that was
    broken for scheduler before this fix."""
    import core.database as db_mod
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db_file = tmp_path / "jarvis.db"
    engine = create_engine(f"sqlite:///{db_file}", future=True)
    db_mod.Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)

    # Route the hook's ``get_db`` call to our isolated engine. We use a
    # generator function so it matches the real ``get_db`` signature
    # (which yields a session and closes it after).
    def _fake_get_db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(db_mod, "get_db", _fake_get_db)

    hooks = create_token_persistence_hooks()
    runner = SimpleNamespace(_agent=_fake_agent_with_usage(input_tokens=555, output_tokens=42))

    token = current_run_id.set("cron-42-deadbeef")
    try:
        await hooks.after_llm_call(runner, MagicMock())
    finally:
        current_run_id.reset(token)

    # Verify the row landed
    sess = SessionLocal()
    try:
        rows = sess.query(db_mod.TokenUsageRecord).all()
    finally:
        sess.close()

    assert len(rows) == 1, f"expected exactly 1 row; got {len(rows)}"
    row = rows[0]
    assert row.run_id == "cron-42-deadbeef"
    assert row.agent_name == "Jarvis"
    assert row.input_tokens == 555
    assert row.output_tokens == 42
    assert row.total_tokens == 597
