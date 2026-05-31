"""
Approval API routes — REST endpoints for approval request management.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from core.auth import verify_api_key
from helpers.http_errors import safe_500
from services.approval_service import approval_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/approvals", tags=["approvals"], dependencies=[Depends(verify_api_key)])


# --- Request/Response Models ---

class CreateApprovalRequest(BaseModel):
    agent_name: str
    team_name: Optional[str] = None
    run_id: Optional[str] = None
    conversation_id: Optional[str] = None
    approval_type: str = "custom"
    title: str
    content: str
    content_format: str = "text"
    urgency: str = "normal"
    impact: Optional[dict] = None
    previous_id: Optional[str] = None
    metadata: Optional[dict] = None


class ResolveApprovalRequest(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$")
    comment: Optional[str] = None


class AddCommentRequest(BaseModel):
    line_number: Optional[int] = None
    selection: Optional[dict] = None
    author: str = "user"
    body: str


class UpdateCommentRequest(BaseModel):
    body: str


# --- Endpoints ---

@router.post("", status_code=201)
async def create_approval(req: CreateApprovalRequest):
    """Agent creates an approval request. Pauses all specified agents."""
    try:
        result = approval_service.create_approval(req.model_dump())
        return result
    except Exception as e:
        raise safe_500(e, logger, "approval_create_failed") from e


@router.get("")
async def list_approvals(status: Optional[str] = None, type: Optional[str] = None):
    """List approval requests with optional filters."""
    try:
        return approval_service.list_approvals(status=status, approval_type=type)
    except Exception as e:
        raise safe_500(e, logger, "approval_list_failed") from e


@router.get("/stats")
async def get_stats():
    """Get approval dashboard stats."""
    try:
        return approval_service.get_stats()
    except Exception as e:
        raise safe_500(e, logger, "approval_stats_failed") from e


@router.get("/{approval_id}")
async def get_approval(approval_id: str):
    """Get approval detail with all comments."""
    result = approval_service.get_approval(approval_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")
    return result


@router.put("/{approval_id}/resolve")
async def resolve_approval(approval_id: str, req: ResolveApprovalRequest):
    """User approves or rejects an approval request. Resumes all paused agents."""
    try:
        return approval_service.resolve_approval(approval_id, req.decision, req.comment)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise safe_500(e, logger, "approval_resolve_failed") from e


@router.post("/{approval_id}/comments", status_code=201)
async def add_comment(approval_id: str, req: AddCommentRequest):
    """Add an inline comment (line click or range selection)."""
    # Validate: must have either line_number or selection
    if req.line_number is None and not req.selection:
        raise HTTPException(status_code=400, detail="Must provide either line_number or selection")
    try:
        return approval_service.add_comment(approval_id, req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise safe_500(e, logger, "approval_add_comment_failed") from e


@router.put("/comments/{comment_id}")
async def update_comment(comment_id: str, req: UpdateCommentRequest):
    """Update an inline comment's body text."""
    try:
        return approval_service.update_comment(comment_id, req.body)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise safe_500(e, logger, "approval_update_comment_failed") from e


@router.delete("/comments/{comment_id}")
async def delete_comment(comment_id: str):
    """Delete an inline comment."""
    try:
        return approval_service.delete_comment(comment_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise safe_500(e, logger, "approval_delete_comment_failed") from e
