"""E2E: the real POST /api/approvals/{id}/comments route enforces the
review-phase lock with the right HTTP status.

- pending approval  → 201 (comment accepted)
- resolved approval → 409 Conflict (it exists but the thread is closed —
  NOT 404, which would wrongly say the approval is missing).
"""
from __future__ import annotations

import pytest

from services.approval_service import approval_service
from core.database import get_db_session, ApprovalRequestModel


def _cleanup():
    db = get_db_session()
    try:
        db.query(ApprovalRequestModel).filter(
            ApprovalRequestModel.approval_type == "comment_e2e"
        ).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _open_setup_gate(monkeypatch):
    from middleware import setup_gate
    monkeypatch.setattr(setup_gate, "is_setup_complete", lambda: True)


@pytest.fixture()
def _seeded():
    # Earlier SAVEPOINT-isolated service tests can leave a stale pooled
    # connection; dispose so this engine-backed e2e starts clean.
    from core.database import engine
    engine.dispose()
    _cleanup()
    appr = approval_service.create_approval({
        "agent_name": "Jarvis",
        "approval_type": "comment_e2e",
        "title": "Comment lock e2e",
        "content": "line one\nline two",
        "content_format": "text",
        "pause": False,
    })
    yield appr["id"]
    _cleanup()


@pytest.mark.asyncio
async def test_comment_on_pending_returns_201(app_client, _seeded):
    resp = await app_client.post(
        f"/api/approvals/{_seeded}/comments",
        json={"line_number": 1, "body": "looks good"},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_comment_on_resolved_returns_409(app_client, _seeded):
    approval_service.resolve_approval(_seeded, decision="approve")
    resp = await app_client.post(
        f"/api/approvals/{_seeded}/comments",
        json={"line_number": 1, "body": "too late"},
    )
    assert resp.status_code == 409, resp.text
    assert "commenting is closed" in resp.text
