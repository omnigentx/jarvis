"""Generated MCP execution approval is bound to the exact source bytes."""
from __future__ import annotations

import pytest

from services import mcp_code_review as review


@pytest.mark.asyncio
async def test_approval_rechecks_source_after_user_decision(tmp_path, monkeypatch):
    server = tmp_path / "server.py"
    server.write_text("print('safe')\n")
    original, _ = review.candidate_snapshot(tmp_path)

    def change_while_waiting(**kwargs):
        assert original in kwargs["content_md"]
        server.write_text("print('changed')\n")
        return True, "user approved"

    monkeypatch.setattr(review, "request_approval", change_while_waiting)
    accepted, reason = await review.approve_candidate(
        "example", tmp_path, action="execute"
    )
    assert accepted is False
    assert "changed" in reason


@pytest.mark.asyncio
async def test_explicit_rejection_blocks_execution(tmp_path, monkeypatch):
    (tmp_path / "server.py").write_text("print('hello')\n")

    def rejected(**kwargs):
        return False, "user rejected"

    monkeypatch.setattr(review, "request_approval", rejected)
    accepted, reason = await review.approve_candidate(
        "example", tmp_path, action="execute"
    )
    assert accepted is False
    assert reason == "user rejected"


def test_snapshot_rejects_symlinked_code(tmp_path):
    target = tmp_path / "source.py"
    target.write_text("print('x')\n")
    (tmp_path / "server.py").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        review.candidate_snapshot(tmp_path)


def test_frozen_server_does_not_follow_later_source_edits(tmp_path):
    source = tmp_path / "generated"
    source.mkdir()
    server = source / "server.py"
    server.write_text("print('approved')\n")
    frozen = review.freeze_candidate(source, tmp_path / "promoted", "sample")
    server.write_text("print('changed')\n")

    assert (frozen / "server.py").read_text() == "print('approved')\n"
    assert review.candidate_snapshot(source)[0] != review.candidate_snapshot(frozen)[0]
