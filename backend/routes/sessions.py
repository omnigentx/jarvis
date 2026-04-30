"""Session & History routes."""
from typing import Optional

from fastapi import APIRouter, Request, Depends

from core.auth import verify_api_key
from services.shared_state import session_service

router = APIRouter(prefix="/api", tags=["sessions"])


@router.get("/history")
async def get_history(
    conversation_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    _=Depends(verify_api_key),
):
    """Return display-friendly history for a conversation.

    ``agent_name`` is optional — when omitted, the session's primary agent
    (stamped on first send) is used. Supply it explicitly when rendering a
    session from the non-primary side of a multi-agent conversation.
    """
    return session_service.get_display_history(conversation_id, agent_name)


@router.get("/conversations")
async def list_conversations(_=Depends(verify_api_key)):
    return session_service.list_sessions()


@router.post("/conversations")
async def create_conversation(request: Request, _=Depends(verify_api_key)):
    """Explicitly create a new conversation (Fast Agent session)."""
    try:
        data = await request.json()
        title = data.get("title", "New Chat")
    except:
        title = "New Chat"

    result = session_service.create_session(title=title)
    return result


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, _=Depends(verify_api_key)):
    session_service.delete_session(conversation_id)
    return {"status": "deleted", "id": conversation_id}
