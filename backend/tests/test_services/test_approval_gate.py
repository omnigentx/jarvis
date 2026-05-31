"""Unit tests for services.approval_gate.

Three behaviours we MUST keep:

1. Hash-keyed decision memory — once approved, identical content auto-
   proceeds; once rejected, identical content stays rejected (so a
   recurring cron doesn't re-prompt every 5 min after rejection).
2. Pending-coalescing — concurrent gate calls for the same hash join
   the same pending approval instead of stacking duplicates.
3. Timeout — caller never hangs forever; treat timeout as rejection.
"""

import asyncio
import time
import uuid
from unittest.mock import patch

import pytest

from services.approval_gate import (
    _content_hash,
    _find_prior_decision,
    _find_pending_match,
    gate,
)


@pytest.fixture
def isolated_db():
    """Ensure the ``approval_requests`` table exists on the shared test DB
    (conftest already points ``JARVIS_DB_PATH`` at ``data/jarvis.test.db``)
    and clear any rows this test inserted before yielding control to the
    next test.

    We deliberately do NOT ``importlib.reload(database)`` — that would
    rebuild ``SessionLocal`` / ``Base`` / engine in the live module, while
    other modules still hold references to the originals. The mixed state
    surfaced as UNIQUE-constraint / team_name failures across unrelated
    test suites in CI.
    """
    from core import database
    database.init_db()  # idempotent — CREATE TABLE IF NOT EXISTS
    # Clean slate so prior tests' rows don't leak into _find_* lookups.
    db = database.SessionLocal()
    try:
        db.query(database.ApprovalRequestModel).delete()
        db.commit()
    finally:
        db.close()
    yield database
    # Tear down: drop the rows this test inserted so the next test sees a
    # clean approval_requests table.
    db = database.SessionLocal()
    try:
        db.query(database.ApprovalRequestModel).delete()
        db.commit()
    finally:
        db.close()


def _insert_approval(database, *, approval_type, scope_key, content_hash, status, **extra):
    import json
    db = database.SessionLocal()
    try:
        approval_id = str(uuid.uuid4())
        now = time.time()
        row = database.ApprovalRequestModel(
            id=approval_id,
            agent_name=extra.get("agent_name", "Jarvis"),
            team_name=None,
            run_id="",
            conversation_id=None,
            approval_type=approval_type,
            title="test",
            content="payload",
            content_format="markdown",
            urgency="normal",
            status=status,
            paused_agents="[]",
            created_at=now,
            resolved_at=now if status != "pending" else None,
            user_decision="approve" if status == "approved" else ("reject" if status == "rejected" else None),
            metadata_json=json.dumps({
                "scope_key": scope_key,
                "content_hash": content_hash,
            }),
        )
        db.add(row)
        db.commit()
        return approval_id
    finally:
        db.close()


def test_hash_is_deterministic_short():
    h1 = _content_hash("hello")
    h2 = _content_hash("hello")
    h3 = _content_hash("hello2")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 16


def test_find_prior_decision_returns_approved(isolated_db):
    _insert_approval(
        isolated_db,
        approval_type="mcp_install", scope_key="mcp:foo",
        content_hash="deadbeef", status="approved",
    )
    assert _find_prior_decision("mcp_install", "mcp:foo", "deadbeef") == "approved"


def test_find_prior_decision_returns_rejected(isolated_db):
    _insert_approval(
        isolated_db,
        approval_type="cron_first_fire", scope_key="cron:abc",
        content_hash="cafebabe", status="rejected",
    )
    assert _find_prior_decision("cron_first_fire", "cron:abc", "cafebabe") == "rejected"


def test_find_prior_decision_ignores_pending(isolated_db):
    # Pending approvals are NOT prior decisions — they are still ambiguous.
    _insert_approval(
        isolated_db,
        approval_type="mcp_install", scope_key="mcp:bar",
        content_hash="1111", status="pending",
    )
    assert _find_prior_decision("mcp_install", "mcp:bar", "1111") is None


def test_find_pending_match_coalesces(isolated_db):
    pending_id = _insert_approval(
        isolated_db,
        approval_type="mcp_install", scope_key="mcp:baz",
        content_hash="2222", status="pending",
    )
    assert _find_pending_match("mcp_install", "mcp:baz", "2222") == pending_id


def test_find_pending_match_ignores_resolved(isolated_db):
    _insert_approval(
        isolated_db,
        approval_type="mcp_install", scope_key="mcp:qux",
        content_hash="3333", status="approved",
    )
    assert _find_pending_match("mcp_install", "mcp:qux", "3333") is None


@pytest.mark.asyncio
async def test_gate_short_circuits_on_prior_approval(isolated_db):
    _insert_approval(
        isolated_db,
        approval_type="mcp_install", scope_key="mcp:auto",
        content_hash=_content_hash("payload"), status="approved",
    )
    # No prompt should reach approval_service — patch to detect.
    with patch("services.approval_service.approval_service.create_approval") as mock_create:
        ok, reason = await gate(
            approval_type="mcp_install",
            scope_key="mcp:auto",
            content_md="payload",
            title="t",
        )
    assert ok is True
    assert "previously approved" in reason
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_gate_short_circuits_on_prior_rejection(isolated_db):
    _insert_approval(
        isolated_db,
        approval_type="cron_first_fire", scope_key="cron:bad",
        content_hash=_content_hash("evil payload"), status="rejected",
    )
    with patch("services.approval_service.approval_service.create_approval") as mock_create:
        ok, reason = await gate(
            approval_type="cron_first_fire",
            scope_key="cron:bad",
            content_md="evil payload",
            title="t",
        )
    assert ok is False
    assert "previously rejected" in reason
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_gate_creates_and_awaits_when_no_prior(isolated_db):
    fake_resolved = {"id": "x", "user_decision": "approve", "user_comment": None}

    async def _fake_wait(approval_id):
        return fake_resolved

    with patch("services.approval_service.approval_service.create_approval",
               return_value={"id": "x"}) as mock_create, \
         patch("services.approval_service.approval_service.wait_for_resolution",
               side_effect=_fake_wait) as mock_wait:
        ok, reason = await gate(
            approval_type="mcp_install",
            scope_key="mcp:new",
            content_md="fresh payload",
            title="t",
        )
    assert ok is True
    assert reason == "user approved"
    mock_create.assert_called_once()
    mock_wait.assert_called_once_with("x")


@pytest.mark.asyncio
async def test_gate_timeout_returns_rejected(isolated_db):
    async def _hang(approval_id):
        await asyncio.sleep(10)  # longer than gate timeout

    with patch("services.approval_service.approval_service.create_approval",
               return_value={"id": "x"}), \
         patch("services.approval_service.approval_service.wait_for_resolution",
               side_effect=_hang):
        ok, reason = await gate(
            approval_type="mcp_install",
            scope_key="mcp:slow",
            content_md="fresh payload",
            title="t",
            timeout_s=0.1,
        )
    assert ok is False
    assert "timeout" in reason


@pytest.mark.asyncio
async def test_gate_timeout_persists_rejection(isolated_db):
    """Reviewer's MEDIUM finding: on timeout we MUST call resolve_approval
    so the row no longer sits `pending`. Otherwise every subsequent fire
    re-attaches to the same stale pending row via _find_pending_match and
    re-blocks for another full timeout window."""

    async def _hang(approval_id):
        await asyncio.sleep(10)

    with patch("services.approval_service.approval_service.create_approval",
               return_value={"id": "x"}), \
         patch("services.approval_service.approval_service.wait_for_resolution",
               side_effect=_hang), \
         patch("services.approval_service.approval_service.resolve_approval") as mock_resolve:
        await gate(
            approval_type="cron_first_fire",
            scope_key="cron:hang",
            content_md="payload",
            title="t",
            timeout_s=0.05,
        )
    # The timeout path must persist the row as rejected so _find_prior_decision
    # short-circuits future calls.
    mock_resolve.assert_called_once()
    args, kwargs = mock_resolve.call_args
    assert args[0] == "x"
    assert kwargs.get("decision") == "reject"
    assert "timeout" in (kwargs.get("comment") or "")


@pytest.mark.asyncio
async def test_gate_timeout_persist_failure_does_not_mask_timeout(isolated_db):
    """If resolve_approval itself raises (e.g. user resolved manually in
    the race window), the gate should still return the timeout reason —
    don't swallow it behind a 'persist failed' string."""

    async def _hang(approval_id):
        await asyncio.sleep(10)

    def _resolve_fails(*a, **kw):
        raise RuntimeError("already resolved")

    with patch("services.approval_service.approval_service.create_approval",
               return_value={"id": "x"}), \
         patch("services.approval_service.approval_service.wait_for_resolution",
               side_effect=_hang), \
         patch("services.approval_service.approval_service.resolve_approval",
               side_effect=_resolve_fails):
        ok, reason = await gate(
            approval_type="cron_first_fire",
            scope_key="cron:hang2",
            content_md="payload",
            title="t",
            timeout_s=0.05,
        )
    assert ok is False
    assert "timeout" in reason
    assert "persist" not in reason  # caller still sees the real reason
