"""HTTP contract for conversation-scoped team control."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth
from routes.team_work import router
from services import team_work_service as work


def test_team_change_requires_auth_and_conversation_scope(monkeypatch):
    monkeypatch.setattr(auth, "JARVIS_API_KEY", "team-test-key")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    assert client.get("/api/team-work/conversations/chat-1/teams").status_code == 401

    monkeypatch.setattr(work, "list_teams", lambda cid: [{"session_id": "team-a"}])
    headers = {"Authorization": "Bearer team-test-key"}
    response = client.get("/api/team-work/conversations/chat-1/teams", headers=headers)
    assert response.status_code == 200
    assert response.json()["teams"] == [{"session_id": "team-a"}]

    def reject_other_conversation(session_id, conversation_id, *args, **kwargs):
        assert session_id == "team-a"
        if conversation_id != "chat-1":
            raise work.TeamWorkError("Team belongs to another conversation", 403)
        return {"id": "change-1", "status": "pending"}

    monkeypatch.setattr(work, "create_revision", reject_other_conversation)
    denied = client.post(
        "/api/team-work/conversations/chat-2/teams/team-a/changes",
        headers=headers, json={"change_text": "Add tests"},
    )
    assert denied.status_code == 403
