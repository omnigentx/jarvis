"""Conversation-scoped API for team revisions and delivery status."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.auth import verify_api_key
from services import team_work_service as work

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/team-work", tags=["team-work"],
    dependencies=[Depends(verify_api_key)],
)


class ChangeBody(BaseModel):
    change_text: str = Field(min_length=1, max_length=12_000)
    idempotency_key: str | None = Field(default=None, max_length=100)
    expected_revision: int | None = Field(default=None, ge=0)
    source_refs: list[str] = Field(default_factory=list, max_length=20)


def _http_error(exc: work.TeamWorkError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.get("/conversations/{conversation_id}/teams")
async def list_conversation_teams(conversation_id: str) -> dict:
    try:
        return {"teams": work.list_teams(conversation_id)}
    except work.TeamWorkError as exc:
        raise _http_error(exc) from exc


@router.get("/conversations/{conversation_id}/teams/{session_id}/revisions")
async def list_team_revisions(
    conversation_id: str, session_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    before_revision: int | None = Query(default=None, ge=1),
) -> dict:
    try:
        return {"revisions": work.get_team_revisions(
            session_id, conversation_id, limit=limit,
            before_revision=before_revision,
        )}
    except work.TeamWorkError as exc:
        raise _http_error(exc) from exc


@router.post("/conversations/{conversation_id}/teams/{session_id}/changes")
async def submit_team_change(
    conversation_id: str, session_id: str, body: ChangeBody,
) -> JSONResponse:
    try:
        revision = work.create_revision(
            session_id, conversation_id, body.change_text,
            idempotency_key=body.idempotency_key,
            expected_revision=body.expected_revision,
            source_refs=body.source_refs,
        )
        try:
            revision = await work.deliver_revision(revision["id"])
        except Exception as exc:
            logger.warning("[TEAM-WORK] Delivery pending for %s", revision["id"],
                           exc_info=True)
            revision["delivery_error"] = str(exc)
        return JSONResponse(status_code=201 if revision["status"] == "delivered" else 202,
                            content=revision)
    except work.TeamWorkError as exc:
        raise _http_error(exc) from exc


@router.post("/conversations/{conversation_id}/teams/{session_id}/retry")
async def retry_team_changes(conversation_id: str, session_id: str) -> dict:
    try:
        return {"delivered": await work.retry_pending(session_id, conversation_id)}
    except work.TeamWorkError as exc:
        raise _http_error(exc) from exc
