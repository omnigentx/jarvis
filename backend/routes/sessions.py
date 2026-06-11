"""Session & History routes."""
from typing import Optional

from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel, Field

from core.auth import verify_api_key
from services.shared_state import session_service

router = APIRouter(prefix="/api", tags=["sessions"])


class BulkDeleteConversations(BaseModel):
    # Cap mirrors settings.py's bulk endpoint — a generous batch bound that
    # still rejects a runaway payload. Deleting >200 in one go can re-fire.
    ids: list[str] = Field(min_length=1, max_length=200)


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
async def list_conversations(
    agent_name: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    _=Depends(verify_api_key),
):
    """Paginated conversation list, optionally scoped to one agent.

    Returns ``{"items": [...], "total": N}``. ``agent_name`` filters to a
    single agent's conversations (the sidebar shows only the active agent's);
    ``limit``/``offset`` page the result so a user with hundreds of sessions
    doesn't load them all at once.
    """
    return session_service.list_sessions(
        agent_name=agent_name, limit=limit, offset=offset
    )


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


@router.post("/conversations/bulk-delete")
async def bulk_delete_conversations(
    payload: BulkDeleteConversations, _=Depends(verify_api_key)
):
    """Delete many conversations in one request.

    Per-id failures don't abort the batch — unknown/already-deleted ids land
    in ``failed`` (delete_session returns False for them) while the rest are
    removed, so a partially-stale client selection still makes progress.
    """
    deleted: list[str] = []
    failed: list[str] = []
    for conversation_id in payload.ids:
        try:
            if session_service.delete_session(conversation_id):
                deleted.append(conversation_id)
            else:
                failed.append(conversation_id)
        except Exception:
            failed.append(conversation_id)
    return {"deleted": deleted, "failed": failed}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, _=Depends(verify_api_key)):
    session_service.delete_session(conversation_id)
    return {"status": "deleted", "id": conversation_id}
