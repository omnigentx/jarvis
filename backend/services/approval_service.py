"""
Approval Service — business logic for agent approval requests.

Handles CRUD operations, team-wide pause/resume via PauseManager,
and realtime SSE broadcasting via ActivityStreamManager.

Also exposes an in-process pub/sub for resolution events
(:func:`wait_for_resolution`) so MCP subprocesses calling via
``approval.wait`` over Runtime RPC can block on the same event without
polling. Single-process by design — see :data:`_resolution_waiters`.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Optional

from core.database import SessionLocal, ApprovalRequestModel, ApprovalCommentModel
from services.pause_controller import pause_controller
from services.activity_stream import activity_stream_manager

logger = logging.getLogger(__name__)


# In-process pub/sub for approval resolution. Each pending approval
# can have multiple waiters (e.g. team scenarios where >1 agent blocks
# on the same approval). The dict key is approval_id, the value is the
# list of futures to resolve when the approval reaches a terminal state.
#
# Single-process by design: Jarvis is a personal AI assistant — the
# backend is heavily stateful (live FastAgent app, MCP subprocesses,
# UDS runtime-RPC server, in-memory SSE fanout, PauseManager…), all of
# which assume one Python process. Multi-worker would break far more
# than this dict, so the in-memory pub/sub matches the rest of the
# architecture rather than introducing a phantom dependency on Redis.
#
# Backend restart safety still matters and is handled separately: the
# wait handler always re-reads DB state on (re)subscribe, so a restart
# that drops in-memory futures is recoverable — the next call from a
# retrying client sees the resolved DB state and returns immediately.
_resolution_waiters: dict[str, list[asyncio.Future]] = {}


def _signal_resolution(approval_id: str, payload: dict) -> None:
    """Notify any in-process waiters that ``approval_id`` has been
    resolved. Safe to call from sync code on the event-loop thread; uses
    ``call_soon_threadsafe`` to bridge if invoked from elsewhere.
    """
    waiters = _resolution_waiters.pop(approval_id, [])
    for fut in waiters:
        if fut.done():
            continue
        try:
            loop = fut.get_loop()
        except RuntimeError:
            continue
        loop.call_soon_threadsafe(_set_future_result, fut, payload)


def _set_future_result(fut: asyncio.Future, payload: dict) -> None:
    """Idempotent set_result — guards against the future already being
    cancelled (e.g. RPC client disconnected between resolve and signal).
    """
    if not fut.done():
        fut.set_result(payload)


class ApprovalService:
    """Manages approval request lifecycle."""

    def create_approval(self, data: dict) -> dict:
        """Create a new approval request.

        Steps:
        1. Insert DB record
        2. Pause all agents listed in pause_agents (team-wide)
        3. Broadcast SSE events (approval_created + approval_stats)

        Returns the created approval dict.
        """
        approval_id = str(uuid.uuid4())
        now = time.time()

        # Build pause list — auto-detect from team if possible
        pause_agents = []
        team_name = data.get("team_name")
        if team_name:
            # Query spawn_records for all running agents in this team
            try:
                from core.database import SpawnRecordModel
                _db = SessionLocal()
                try:
                    team_members = _db.query(SpawnRecordModel.agent_name).filter(
                        SpawnRecordModel.team_name == team_name,
                        SpawnRecordModel.status.in_(["running", "idle"]),
                    ).all()
                    pause_agents = list(set(m[0] for m in team_members))
                    logger.info("[APPROVAL] Auto-detected %d team members to pause for team '%s': %s",
                                len(pause_agents), team_name, pause_agents)
                finally:
                    _db.close()
            except Exception as e:
                logger.warning("[APPROVAL] Failed to auto-detect team members: %s", e)
        if not pause_agents:
            # Fallback: pause just the requesting agent
            pause_agents = [data["agent_name"]]

        db = SessionLocal()
        try:
            record = ApprovalRequestModel(
                id=approval_id,
                agent_name=data["agent_name"],
                team_name=data.get("team_name"),
                run_id=data.get("run_id", ""),
                conversation_id=data.get("conversation_id"),
                approval_type=data.get("approval_type", "custom"),
                title=data["title"],
                content=data["content"],
                content_format=data.get("content_format", "text"),
                urgency=data.get("urgency", "normal"),
                status="pending",
                impact_files=data.get("impact", {}).get("files") if isinstance(data.get("impact"), dict) else None,
                impact_services=data.get("impact", {}).get("services") if isinstance(data.get("impact"), dict) else None,
                impact_downtime=data.get("impact", {}).get("downtime") if isinstance(data.get("impact"), dict) else None,
                impact_risk=data.get("impact", {}).get("risk") if isinstance(data.get("impact"), dict) else None,
                paused_agents=json.dumps(pause_agents),
                previous_id=data.get("previous_id"),
                created_at=now,
                metadata_json=json.dumps(data.get("metadata")) if data.get("metadata") else None,
            )
            db.add(record)
            db.commit()

            result = self._record_to_dict(record)
        finally:
            db.close()

        # Pause via PauseController. When ``team_name`` is set we pass it
        # as the scope so the controller's ``_resolve_scope`` expands to
        # the live team membership at this moment (handles new joiners
        # between the pre-compute above and now). When there's no team,
        # the requesting agent is a solo pause.
        scope = team_name or data["agent_name"]
        scope_changed = pause_controller.pause(scope)
        paused_count = sum(1 for a in pause_agents if pause_controller.is_paused(a))
        if scope_changed:
            logger.info("[APPROVAL] Paused scope %r for approval %s (%d agents now paused)",
                        scope, approval_id, paused_count)

        logger.info(
            "[APPROVAL] Created %s — type=%s agent=%s urgency=%s paused=%d agents",
            approval_id, data.get("approval_type"), data["agent_name"],
            data.get("urgency", "normal"), paused_count,
        )

        # Broadcast SSE events
        activity_stream_manager.broadcast({
            "agent_name": data["agent_name"],
            "event_type": "approval_created",
            "message": f"Approval requested: {data['title']}",
            "timestamp": now,
            "data": {
                "approval_id": approval_id,
                "title": data["title"],
                "agent_name": data["agent_name"],
                "team_name": data.get("team_name"),
                "urgency": data.get("urgency", "normal"),
                "approval_type": data.get("approval_type", "custom"),
                "paused_agents": pause_agents,
            },
        })
        self._broadcast_stats()

        return result

    def resolve_approval(self, approval_id: str, decision: str, comment: Optional[str] = None) -> dict:
        """Resolve an approval request (approve/reject).

        Steps:
        1. Update DB record
        2. Resume all paused agents
        3. Broadcast SSE events (approval_resolved + approval_stats)

        Returns the updated approval dict.
        """
        db = SessionLocal()
        try:
            record = db.query(ApprovalRequestModel).filter_by(id=approval_id).first()
            if not record:
                raise ValueError(f"Approval {approval_id} not found")
            if record.status != "pending":
                raise ValueError(f"Approval {approval_id} already resolved: {record.status}")

            now = time.time()
            record.status = "approved" if decision == "approve" else "rejected"
            record.user_decision = decision
            record.user_comment = comment
            record.resolved_at = now
            db.commit()

            result = self._record_to_dict(record)
            paused_agents = json.loads(record.paused_agents or "[]")
        finally:
            db.close()

        # Resume via PauseController scope. Mirror of create path.
        scope = result.get("team_name") or result["agent_name"]
        before_paused = {a for a in paused_agents if pause_controller.is_paused(a)}
        pause_controller.resume(scope)
        resumed_count = sum(1 for a in before_paused if not pause_controller.is_paused(a))
        if resumed_count:
            logger.info("[APPROVAL] Resumed scope %r after %s (%d agents resumed)",
                        scope, decision, resumed_count)

        logger.info(
            "[APPROVAL] Resolved %s — decision=%s resumed=%d agents",
            approval_id, decision, resumed_count,
        )

        # Broadcast SSE events
        activity_stream_manager.broadcast({
            "agent_name": result["agent_name"],
            "event_type": "approval_resolved",
            "message": f"Approval {decision}: {result['title']}",
            "timestamp": now,
            "data": {
                "approval_id": approval_id,
                "decision": decision,
                "comment": comment,
                "agent_name": result["agent_name"],
                "team_name": result.get("team_name"),
                "resumed_agents": paused_agents,
            },
        })
        self._broadcast_stats()

        # Fetch inline comments to include in result (for MCP tool)
        result["inline_comments"] = self._get_comments(approval_id)

        # Notify in-process waiters (Runtime RPC ``approval.wait``
        # subscribers). Done last so waiters see the final dict, with
        # comments included.
        _signal_resolution(approval_id, dict(result))

        return result

    async def wait_for_resolution(self, approval_id: str) -> dict:
        """Block until ``approval_id`` reaches a terminal state, then
        return the resolved approval dict (including inline comments).

        Used by the Runtime RPC ``approval.wait`` handler. Safe against
        the resolve-before-subscribe race: subscribes first, then
        re-checks DB state under the same coroutine to drain ourselves
        if resolution happened in the gap.

        Raises ``KeyError`` if the approval id is unknown.
        """
        record = self.get_approval(approval_id)
        if record is None:
            raise KeyError(approval_id)
        if record["status"] != "pending":
            # Already resolved — return immediately. Match the dict shape
            # produced by ``resolve_approval`` so callers don't need a
            # second branch (they can rely on ``inline_comments`` being
            # present alongside ``user_decision``).
            record["inline_comments"] = record.get("comments", [])
            return record

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        _resolution_waiters.setdefault(approval_id, []).append(fut)

        try:
            # Re-check DB state. Resolution could have happened between
            # the first check above and the subscribe-list append. If it
            # already landed, _signal_resolution will have popped the
            # entry before we appended → our future never fires → so we
            # must bail out by reading state ourselves.
            record = self.get_approval(approval_id)
            if record is not None and record["status"] != "pending":
                record["inline_comments"] = record.get("comments", [])
                return record

            return await fut
        finally:
            waiters = _resolution_waiters.get(approval_id, [])
            try:
                waiters.remove(fut)
            except ValueError:
                pass
            if not waiters:
                _resolution_waiters.pop(approval_id, None)

    def add_comment(self, approval_id: str, data: dict) -> dict:
        """Add an inline comment to an approval request.

        Supports two modes:
        - Line click: data has 'line_number'
        - Range selection: data has 'selection' dict
        """
        db = SessionLocal()
        try:
            # Verify approval exists
            record = db.query(ApprovalRequestModel).filter_by(id=approval_id).first()
            if not record:
                raise ValueError(f"Approval {approval_id} not found")

            # Extract values before session close (avoid DetachedInstanceError)
            agent_name = record.agent_name
            title = record.title

            comment_id = str(uuid.uuid4())
            now = time.time()
            selection = data.get("selection") or {}

            comment = ApprovalCommentModel(
                id=comment_id,
                approval_id=approval_id,
                line_number=data.get("line_number"),
                selection_start_line=selection.get("start_line"),
                selection_end_line=selection.get("end_line"),
                selection_start_offset=selection.get("start_offset"),
                selection_end_offset=selection.get("end_offset"),
                selected_text=selection.get("selected_text"),
                author=data.get("author", "user"),
                body=data["body"],
                created_at=now,
            )
            db.add(comment)
            db.commit()

            result = self._comment_to_dict(comment)
        finally:
            db.close()

        # Broadcast SSE event (using extracted values, not ORM record)
        activity_stream_manager.broadcast({
            "agent_name": agent_name,
            "event_type": "approval_commented",
            "message": f"Comment on approval: {title}",
            "timestamp": now,
            "data": {
                "approval_id": approval_id,
                "comment_id": comment_id,
                "line_number": data.get("line_number"),
                "selection": selection if selection else None,
                "body": data["body"],
            },
        })

        return result

    def update_comment(self, comment_id: str, body: str) -> dict:
        """Update an existing inline comment's body text."""
        db = SessionLocal()
        try:
            comment = db.query(ApprovalCommentModel).filter_by(id=comment_id).first()
            if not comment:
                raise ValueError(f"Comment {comment_id} not found")
            comment.body = body
            db.commit()
            result = self._comment_to_dict(comment)
            approval_id = comment.approval_id
        finally:
            db.close()

        logger.info("[APPROVAL] Updated comment %s", comment_id)
        return result

    def delete_comment(self, comment_id: str) -> dict:
        """Delete an inline comment. Returns the deleted comment info."""
        db = SessionLocal()
        try:
            comment = db.query(ApprovalCommentModel).filter_by(id=comment_id).first()
            if not comment:
                raise ValueError(f"Comment {comment_id} not found")
            result = self._comment_to_dict(comment)
            approval_id = comment.approval_id
            db.delete(comment)
            db.commit()
        finally:
            db.close()

        logger.info("[APPROVAL] Deleted comment %s from approval %s", comment_id, approval_id)
        return result

    def list_approvals(self, status: Optional[str] = None, approval_type: Optional[str] = None) -> list[dict]:
        """List approval requests with optional filters."""
        db = SessionLocal()
        try:
            query = db.query(ApprovalRequestModel)
            if status:
                query = query.filter_by(status=status)
            if approval_type:
                query = query.filter_by(approval_type=approval_type)
            query = query.order_by(ApprovalRequestModel.created_at.desc())
            records = query.limit(200).all()
            return [self._record_to_dict(r) for r in records]
        finally:
            db.close()

    def get_approval(self, approval_id: str) -> Optional[dict]:
        """Get approval detail with all comments."""
        db = SessionLocal()
        try:
            record = db.query(ApprovalRequestModel).filter_by(id=approval_id).first()
            if not record:
                return None
            result = self._record_to_dict(record)
            result["comments"] = self._get_comments(approval_id, db=db)
            return result
        finally:
            db.close()

    def get_stats(self) -> dict:
        """Compute dashboard stats."""
        db = SessionLocal()
        try:
            pending = db.query(ApprovalRequestModel).filter_by(status="pending").count()
            
            # Approved today
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            approved_today = db.query(ApprovalRequestModel).filter(
                ApprovalRequestModel.status == "approved",
                ApprovalRequestModel.resolved_at >= today_start,
            ).count()
            
            rejected = db.query(ApprovalRequestModel).filter_by(status="rejected").count()
            
            # Average response time (for resolved approvals)
            from sqlalchemy import func
            avg_result = db.query(
                func.avg(ApprovalRequestModel.resolved_at - ApprovalRequestModel.created_at)
            ).filter(
                ApprovalRequestModel.resolved_at.isnot(None)
            ).scalar()
            avg_response_time = round(avg_result, 1) if avg_result else 0

            return {
                "pending_count": pending,
                "approved_today": approved_today,
                "rejected_count": rejected,
                "avg_response_time": avg_response_time,
            }
        finally:
            db.close()

    def restore_pending_on_startup(self) -> int:
        """On server restart: re-register paused agents from pending approvals.

        Returns count of agents re-paused.
        """
        db = SessionLocal()
        try:
            pending = db.query(ApprovalRequestModel).filter_by(status="pending").all()
            if not pending:
                return 0

            count = 0
            for record in pending:
                agents = json.loads(record.paused_agents or "[]")
                for agent_name in agents:
                    # Restore per-agent (not scope) because the stored list
                    # is the authoritative record of who was paused at
                    # approval-create time. ``pause(agent_name)`` will
                    # still expand to the team via _resolve_scope if the
                    # agent is in one — handled idempotently by the
                    # controller for agents already in the per-agent list.
                    pause_controller.pause(agent_name)
                    count += 1
                    logger.info("[APPROVAL] Restored pause for %s (approval=%s)", agent_name, record.id)
            
            logger.info("[APPROVAL] Restored %d paused agents from %d pending approvals", count, len(pending))
            return count
        finally:
            db.close()

    def _broadcast_stats(self) -> None:
        """Broadcast current stats via SSE (piggyback on every event)."""
        stats = self.get_stats()
        activity_stream_manager.broadcast({
            "agent_name": "__system__",
            "event_type": "approval_stats",
            "message": f"Approval stats update",
            "timestamp": time.time(),
            "data": stats,
        })

    def _get_comments(self, approval_id: str, db=None) -> list[dict]:
        """Get all comments for an approval."""
        owned_db = db is None
        if owned_db:
            db = SessionLocal()
        try:
            comments = db.query(ApprovalCommentModel).filter_by(
                approval_id=approval_id
            ).order_by(ApprovalCommentModel.created_at.asc()).all()
            return [self._comment_to_dict(c) for c in comments]
        finally:
            if owned_db:
                db.close()

    @staticmethod
    def _record_to_dict(record: ApprovalRequestModel) -> dict:
        return {
            "id": record.id,
            "agent_name": record.agent_name,
            "team_name": record.team_name,
            "run_id": record.run_id,
            "conversation_id": record.conversation_id,
            "approval_type": record.approval_type,
            "title": record.title,
            "content": record.content,
            "content_format": record.content_format,
            "urgency": record.urgency,
            "status": record.status,
            "impact_files": record.impact_files,
            "impact_services": record.impact_services,
            "impact_downtime": record.impact_downtime,
            "impact_risk": record.impact_risk,
            "user_decision": record.user_decision,
            "user_comment": record.user_comment,
            "paused_agents": json.loads(record.paused_agents or "[]"),
            "previous_id": record.previous_id,
            "created_at": record.created_at,
            "resolved_at": record.resolved_at,
        }

    @staticmethod
    def _comment_to_dict(comment: ApprovalCommentModel) -> dict:
        result = {
            "id": comment.id,
            "approval_id": comment.approval_id,
            "author": comment.author,
            "body": comment.body,
            "created_at": comment.created_at,
        }
        # Include whichever mode was used
        if comment.line_number is not None:
            result["line_number"] = comment.line_number
        if comment.selection_start_line is not None:
            result["selection"] = {
                "start_line": comment.selection_start_line,
                "end_line": comment.selection_end_line,
                "start_offset": comment.selection_start_offset,
                "end_offset": comment.selection_end_offset,
                "selected_text": comment.selected_text,
            }
        return result


# Singleton
approval_service = ApprovalService()
