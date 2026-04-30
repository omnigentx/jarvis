"""
Notification Inbox REST API.

Endpoints:
  GET    /api/notifications           — List (paginated, filterable)
  GET    /api/notifications/:id       — Single detail
  PATCH  /api/notifications/:id/read  — Mark read
  PATCH  /api/notifications/:id/unread — Mark unread
  POST   /api/notifications/mark-all-read — Bulk mark all read
  GET    /api/notifications/unread-count — Badge count
  DELETE /api/notifications/:id       — Delete single
"""
import json
import logging
import time

from fastapi import APIRouter, Depends, Query, HTTPException
from core.auth import verify_api_key
from core.database import get_db_session, NotificationModel

logger = logging.getLogger("notifications_api")

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _format_notification(n: NotificationModel, full: bool = False) -> dict:
    """Format notification for API response."""
    data = {
        "id": n.id,
        "run_id": n.run_id,
        "job_id": n.job_id,
        "type": n.type,
        "title": n.title,
        "preview": n.preview,
        "content_type": n.content_type,
        "is_read": bool(n.is_read),
        "created_at": n.created_at,
        "read_at": n.read_at,
        "metadata": json.loads(n.metadata_json) if n.metadata_json else None,
    }
    if full:
        data["content"] = n.content
    return data


# ─── unread-count (must be before /:id to avoid route conflict) ───

@router.get("/unread-count", dependencies=[Depends(verify_api_key)])
async def get_unread_count():
    db = get_db_session()
    try:
        count = db.query(NotificationModel).filter(NotificationModel.is_read == 0).count()
        return {"unread_count": count}
    finally:
        db.close()


# ─── mark-all-read ───

@router.post("/mark-all-read", dependencies=[Depends(verify_api_key)])
async def mark_all_read():
    db = get_db_session()
    try:
        now = time.time()
        updated = (
            db.query(NotificationModel)
            .filter(NotificationModel.is_read == 0)
            .update({"is_read": 1, "read_at": now})
        )
        db.commit()
        return {"updated": updated}
    finally:
        db.close()


# ─── List ───

@router.get("", dependencies=[Depends(verify_api_key)])
async def list_notifications(
    type: str = Query(None, description="Filter by type: reminder, agent_result, error"),
    is_read: int = Query(None, description="Filter: 0=unread, 1=read"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    db = get_db_session()
    try:
        q = db.query(NotificationModel)
        if type:
            q = q.filter(NotificationModel.type == type)
        if is_read is not None:
            q = q.filter(NotificationModel.is_read == is_read)

        total = q.count()
        items = (
            q.order_by(NotificationModel.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return {
            "items": [_format_notification(n) for n in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        db.close()


# ─── Detail ───

@router.get("/{notification_id}", dependencies=[Depends(verify_api_key)])
async def get_notification(notification_id: int):
    db = get_db_session()
    try:
        n = db.query(NotificationModel).filter(NotificationModel.id == notification_id).first()
        if not n:
            raise HTTPException(status_code=404, detail="Notification not found")
        return _format_notification(n, full=True)
    finally:
        db.close()


# ─── Mark Read ───

@router.patch("/{notification_id}/read", dependencies=[Depends(verify_api_key)])
async def mark_read(notification_id: int):
    db = get_db_session()
    try:
        n = db.query(NotificationModel).filter(NotificationModel.id == notification_id).first()
        if not n:
            raise HTTPException(status_code=404, detail="Notification not found")
        n.is_read = 1
        n.read_at = time.time()
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ─── Mark Unread ───

@router.patch("/{notification_id}/unread", dependencies=[Depends(verify_api_key)])
async def mark_unread(notification_id: int):
    db = get_db_session()
    try:
        n = db.query(NotificationModel).filter(NotificationModel.id == notification_id).first()
        if not n:
            raise HTTPException(status_code=404, detail="Notification not found")
        n.is_read = 0
        n.read_at = None
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ─── Delete ───

@router.delete("/{notification_id}", dependencies=[Depends(verify_api_key)])
async def delete_notification(notification_id: int):
    db = get_db_session()
    try:
        n = db.query(NotificationModel).filter(NotificationModel.id == notification_id).first()
        if not n:
            raise HTTPException(status_code=404, detail="Notification not found")
        db.delete(n)
        db.commit()
        return {"ok": True}
    finally:
        db.close()
