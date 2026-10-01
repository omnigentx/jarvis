from unittest.mock import Mock

import pytest

from services.plugins.installation import approve_content, approve_source


@pytest.mark.asyncio
async def test_content_review_identifies_requester_and_destination(monkeypatch):
    request = Mock(return_value=(False, "pending"))
    monkeypatch.setattr("services.approval_gate.request_approval", request)
    record = {
        "id": "candidate",
        "repo": "anthropics/claude-code",
        "commit": "a" * 40,
        "subdirectory": "plugins/frontend-design",
        "name": "frontend-design",
        "digest": "digest",
        "skills": [],
        "blockers": [],
        "target_agent": "team:scope",
        "target_label": "Hayden [Dev]",
        "requested_by": "Sawyer [PM]",
    }
    assert not await approve_content(record)
    kwargs = request.call_args.kwargs
    assert kwargs["agent_name"] == "Sawyer [PM]"
    assert "Hayden [Dev]" in kwargs["content_md"]
    assert kwargs["scope_key"] == "plugin:candidate:team:scope:digest"


@pytest.mark.asyncio
async def test_source_review_attribution_does_not_change_source_identity(monkeypatch):
    request = Mock(return_value=(False, "pending"))
    monkeypatch.setattr("services.approval_gate.request_approval", request)
    source = {
        "repo": "anthropics/claude-code",
        "commit": "a" * 40,
        "subdirectory": "plugins/frontend-design",
    }
    await approve_source(source, requested_by="Sawyer [PM]")
    kwargs = request.call_args.kwargs
    assert kwargs["agent_name"] == "Sawyer [PM]"
    assert "Sawyer" not in kwargs["content_md"]
