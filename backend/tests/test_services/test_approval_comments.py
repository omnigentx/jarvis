"""Inline comments close once an approval is resolved.

Comments belong to the review phase. The UI hides the composer after
approve/reject, but the server is the real gate — a direct API call must not be
able to append a comment to a resolved approval.
"""
from __future__ import annotations

import pytest

from services.approval_service import approval_service, ApprovalConflictError


@pytest.fixture(autouse=True)
def _isolate_db(mcp_db_isolation):
    yield


def _make_pending() -> str:
    appr = approval_service.create_approval({
        "agent_name": "Jarvis",
        "approval_type": "custom",
        "title": "Plan review",
        "content": "line one\nline two\nline three",
        "content_format": "text",
        "pause": False,  # deferred gate — no live agent to pause
    })
    return appr["id"]


def test_comment_allowed_while_pending():
    aid = _make_pending()
    out = approval_service.add_comment(aid, {"line_number": 1, "body": "looks good", "author": "user"})
    assert out and out.get("body") == "looks good"


def test_comment_blocked_after_approve():
    aid = _make_pending()
    approval_service.resolve_approval(aid, decision="approve")
    with pytest.raises(ApprovalConflictError, match="commenting is closed"):
        approval_service.add_comment(aid, {"line_number": 2, "body": "too late", "author": "user"})


def test_comment_blocked_after_reject():
    aid = _make_pending()
    approval_service.resolve_approval(aid, decision="reject")
    with pytest.raises(ApprovalConflictError, match="commenting is closed"):
        approval_service.add_comment(aid, {"line_number": 2, "body": "too late", "author": "user"})
