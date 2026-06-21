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


class ApprovalConflictError(ValueError):
    """The approval exists but is in a state that forbids the action (e.g.
    commenting on an already-resolved approval). Subclasses ValueError so
    existing ``except ValueError`` callers still catch it, while the route can
    map it to HTTP 409 Conflict (not 404 — the approval is not missing)."""


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

        # Enforce ONE pending cron_approval card per job. If the agent re-gates
        # a job (edited payload / agent / schedule) while an OLDER card is still
        # pending, two cards would point at the same job — and approving the
        # STALE one would vet old bytes while the job runs the NEW ones
        # (vet-v1-run-v2). Supersede the stale card(s) so only this fresh one,
        # which shows the current payload, is actionable.
        if data.get("approval_type") == "cron_approval":
            _job_id = (data.get("metadata") or {}).get("job_id")
            if _job_id:
                self._supersede_pending_cron_cards(_job_id)

        # Resolve team membership AUTHORITATIVELY from ``spawn_records``.
        # ``team_name`` arrives from the MCP tool as a free-text LLM
        # parameter — the LLM frequently picks the workspace basename
        # (``agile-team_<session>``) instead of the logical team_name
        # (``tool-audit-approval-team``). We do not trust it. Instead
        # we look up the requesting agent's real ``team_name`` from its
        # spawn_records row (single source of truth), then list every
        # team member from that. The LLM-supplied value is ignored
        # — and if it disagrees with the truth, we fail loud so the
        # operator can fix the prompt that's confusing the model.
        agent_name = data["agent_name"]
        llm_team_name = data.get("team_name")

        from core.database import SpawnRecordModel
        team_name: str | None = None
        pause_agents: list[str]

        # ``pause=False`` opts out of the team-pause machinery entirely.
        # Used by deferred gates (e.g. cron creation-time approval) where the
        # approval is NOT blocking a live agent turn — the requesting agent
        # may be Jarvis mid-conversation with the user, and pausing it would
        # freeze that chat. These approvals just sit in the inbox until
        # resolved; nothing is held.
        if not data.get("pause", True):
            pause_agents = []
            return self._persist_and_broadcast(
                approval_id, now, data, team_name=None, pause_agents=pause_agents,
            )

        # Single session for both lookups (team_name + team members).
        # Was two consecutive SessionLocal() blocks; merged because they
        # don't need transactional isolation between them and the second
        # open + close was pure overhead.
        _db = SessionLocal()
        try:
            requester_row = _db.query(SpawnRecordModel.team_name).filter(
                SpawnRecordModel.agent_name == agent_name,
            ).first()
            if requester_row:
                team_name = requester_row[0]  # may be None for solo spawns

            if llm_team_name and llm_team_name != team_name:
                # Fail loud: refuse rather than silently substituting. The
                # LLM is passing wrong context — the symptom is the wrong
                # team gets paused (or none at all). Surface to the LLM via
                # the MCP tool's error path so the prompt designer notices.
                raise ValueError(
                    f"approval.create rejected: supplied team_name={llm_team_name!r} "
                    f"does not match agent {agent_name!r}'s actual team={team_name!r}. "
                    f"Pass team_name from your spawn config or omit it."
                )

            if team_name:
                # Real team — list every running/idle member.
                team_members = _db.query(SpawnRecordModel.agent_name).filter(
                    SpawnRecordModel.team_name == team_name,
                    SpawnRecordModel.status.in_(["running", "idle"]),
                ).all()
                pause_agents = list({m[0] for m in team_members})
                # Defensive: requester must be in the list. If their row
                # status isn't running/idle (race with completion) the
                # query misses them — add explicitly so we don't ship an
                # approval whose owner isn't paused.
                if agent_name not in pause_agents:
                    pause_agents.append(agent_name)
                logger.info("[APPROVAL] Team %r → pausing %d members: %s",
                            team_name, len(pause_agents), pause_agents)
            else:
                # Solo agent (in-process Jarvis or ad-hoc spawn with no team).
                pause_agents = [agent_name]
                logger.info("[APPROVAL] Solo agent %r → pausing self only", agent_name)
        finally:
            _db.close()

        return self._persist_and_broadcast(
            approval_id, now, data, team_name=team_name, pause_agents=pause_agents,
        )

    def _persist_and_broadcast(
        self, approval_id: str, now: float, data: dict, *,
        team_name: Optional[str], pause_agents: list,
    ) -> dict:
        """Insert the approval row, pause ``pause_agents`` (may be empty),
        and broadcast SSE. Shared by the normal team-pause path and the
        ``pause=False`` deferred-gate path so both produce identical rows
        and events."""
        db = SessionLocal()
        try:
            record = ApprovalRequestModel(
                id=approval_id,
                agent_name=data["agent_name"],
                team_name=team_name,  # authoritative (from spawn_records), not LLM-supplied
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

        # Pause via PauseController. We iterate the AUTHORITATIVE
        # ``pause_agents`` list (pre-computed above from spawn_records)
        # rather than passing the LLM-supplied ``team_name`` as scope.
        #
        # Why: the MCP tool accepts ``team_name`` as a free-text param
        # filled by the LLM. The LLM frequently picks the workspace
        # basename (``agile-team_<session>``) instead of the real logical
        # team_name (e.g. ``tool-audit-approval-team``). Passing that
        # bogus value as scope to ``pause_controller.pause`` triggers
        # the "solo agent" fallback in ``_resolve_scope`` — it would
        # pause a non-existent agent named ``agile-team_<session>`` and
        # leave the actual PM/members untouched. ``pause_agents`` was
        # computed by querying ``spawn_records.team_name`` directly, so
        # it reflects ground truth regardless of what the LLM thought.
        paused_count = 0
        for agent_name in pause_agents:
            if pause_controller.pause(agent_name):
                paused_count += 1
        if paused_count:
            logger.info("[APPROVAL] Paused %d agent(s) for approval %s: %s",
                        paused_count, approval_id, pause_agents)

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

    def _supersede_pending_cron_cards(self, job_id: str) -> None:
        """Mark any still-pending cron_approval card(s) for ``job_id`` as
        ``cancelled`` so a fresh card (vetting the current payload) is the only
        actionable one. Cleanup ONLY — deliberately does NOT write through to
        ``CronJobModel.approval_status``: superseding a stale card is not a user
        decision, and the re-gate that triggered this already set the job back
        to ``pending``. Broadcasts ``approval_resolved`` so the inbox clears the
        stale card live."""
        db = SessionLocal()
        superseded: list[tuple] = []
        try:
            cards = db.query(ApprovalRequestModel).filter(
                ApprovalRequestModel.approval_type == "cron_approval",
                ApprovalRequestModel.status == "pending",
            ).all()
            now = time.time()
            for c in cards:
                try:
                    meta = json.loads(c.metadata_json or "{}")
                except (TypeError, ValueError):
                    meta = {}
                if meta.get("job_id") == job_id:
                    c.status = "cancelled"
                    c.resolved_at = now
                    superseded.append((c.id, c.agent_name, c.title))
            if superseded:
                db.commit()
        finally:
            db.close()

        for cid, agent_name, title in superseded:
            activity_stream_manager.broadcast({
                "agent_name": agent_name,
                "event_type": "approval_resolved",
                "message": f"Approval superseded: {title}",
                "timestamp": time.time(),
                "data": {
                    "approval_id": cid,
                    "decision": "cancelled",
                    "agent_name": agent_name,
                },
            })
        if superseded:
            self._broadcast_stats()
            logger.info("[APPROVAL] Superseded %d stale cron card(s) for job %s",
                        len(superseded), job_id)

    def _apply_cron_decision(self, job_id: Optional[str], decision: str) -> None:
        """Mirror an approval decision onto the cron job's approval_status
        (the scheduler's source of truth) and wake the scheduler so an
        approved job is re-evaluated immediately rather than on the next tick.
        Best-effort: a missing job_id/job is logged, not raised — the
        approval itself is already resolved."""
        if not job_id:
            logger.warning("[APPROVAL] cron_approval resolved with no job_id in metadata")
            return
        from core.database import CronJobModel
        db = SessionLocal()
        try:
            job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
            if not job:
                logger.warning("[APPROVAL] cron_approval job %s not found", job_id)
                return
            job.approval_status = "approved" if decision == "approve" else "rejected"
            job.updated_at = time.time()
            db.commit()
            new_status = job.approval_status
            job_name = job.name
            logger.info("[APPROVAL] cron job %s approval_status → %s", job_id, new_status)
        finally:
            db.close()
        try:
            from services.cron_scheduler import cron_scheduler
            # Push to the Scheduler dashboard so the pending badge flips live.
            cron_scheduler._broadcast_event("job_approval_changed", {
                "job_id": job_id,
                "job_name": job_name,
                "approval_status": new_status,
            })
            if decision == "approve":
                cron_scheduler.wake()  # re-evaluate now instead of next tick
        except Exception as exc:  # scheduler may be down in tests
            logger.debug("[APPROVAL] could not notify scheduler: %s", exc)

    def _apply_memory_candidate_decision(self, candidate_id: Optional[str], decision: str) -> None:
        """Mirror an approval decision onto the memory candidate (the SSoT).
        Best-effort: a missing candidate is logged, not raised — the approval
        itself is already resolved."""
        if not candidate_id:
            logger.warning("[APPROVAL] memory_candidate resolved with no candidate_id")
            return
        from services.memory import candidate_service
        db = SessionLocal()
        try:
            # _from_approval=True: this resolution ORIGINATED from the inbox, so
            # candidate_service must NOT loop back to close the card (we already
            # flipped it above). Prevents inbox→candidate→card re-entrancy.
            if decision == "approve":
                candidate_service.approve_candidate(db, candidate_id, _from_approval=True)
            else:
                candidate_service.reject_candidate(db, candidate_id,
                                                   reason="rejected via approvals", _from_approval=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[APPROVAL] memory candidate %s apply failed: %s", candidate_id, exc)
        finally:
            db.close()

    def resolve_memory_candidate_card(self, candidate_id: Optional[str], decision: str) -> None:
        """Close the inbox card linked to a memory candidate that was resolved
        on the Memory page, so the sidebar badge + Approvals list don't show a
        stale pending card. No-op if there's no pending card. Calls
        ``resolve_approval`` (which re-enters candidate_service with
        ``_from_approval=True`` on the now-resolved candidate — idempotent), so
        this terminates without looping."""
        if not candidate_id:
            return
        db = SessionLocal()
        try:
            rows = db.query(ApprovalRequestModel).filter_by(
                approval_type="memory_candidate", status="pending").all()
            target_id = None
            for r in rows:
                meta = json.loads(r.metadata_json or "{}")
                if meta.get("candidate_id") == candidate_id:
                    target_id = r.id
                    break
        finally:
            db.close()
        if target_id:
            try:
                self.resolve_approval(target_id, decision)
            except ValueError:
                pass  # already resolved (race) — fine

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
            # Captured here (while the row is loaded) for the cron write-through
            # below — _record_to_dict intentionally omits metadata_json.
            resolved_type = record.approval_type
            resolved_meta = json.loads(record.metadata_json or "{}")
        finally:
            db.close()

        # Write-through for deferred cron gates: the ApprovalRequest card is
        # just the user's inbox surface — CronJobModel.approval_status is the
        # SINGLE SOURCE OF TRUTH the scheduler reads at fire time. Mirror the
        # decision onto the job so it can (or can't) fire. Done here so BOTH
        # resolution surfaces (Approvals page, RPC) stay consistent through
        # one write path.
        if resolved_type == "cron_approval":
            self._apply_cron_decision(resolved_meta.get("job_id"), decision)

        # Same write-through pattern for memory candidates: the approval card
        # is just the inbox surface; ``memory_candidates.status`` is the SSoT.
        # Resolution here flows back into candidate_service so BOTH surfaces
        # (Approvals page + Memory page) stay consistent through one path.
        if resolved_type == "memory_candidate":
            self._apply_memory_candidate_decision(
                resolved_meta.get("candidate_id"), decision)

        # Resume via PauseController — iterate the authoritative
        # ``paused_agents`` list stored on the approval row. This
        # approval was already flipped to ``approved/rejected`` above
        # so its row no longer counts toward ``_pending_approval_for``.
        #
        # Multi-approval correctness: an agent may also be held by a
        # DIFFERENT still-pending approval (e.g. two parallel team
        # workflows). In that case ``pause_controller.resume`` raises
        # ``PauseProtected`` — we catch and skip silently so this
        # cascade doesn't undo the other approval's hold. The agent
        # will resume when the LAST holding approval resolves.
        from services.pause_controller import PauseProtected
        resumed_count = 0
        for agent_name in paused_agents:
            try:
                if pause_controller.resume(agent_name):
                    resumed_count += 1
            except PauseProtected as exc:
                logger.info(
                    "[APPROVAL] %s still held by approval %s — keeping paused",
                    agent_name, exc.approval_id,
                )
        if resumed_count:
            logger.info("[APPROVAL] Resumed %d agent(s) after %s: %s",
                        resumed_count, decision, paused_agents)

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

            # Comments belong to the review phase. Once approved/rejected the
            # thread is closed — reject server-side so a direct API call can't
            # append after resolution (the UI already hides the composer).
            if record.status != "pending":
                raise ApprovalConflictError(
                    f"Approval {approval_id} is {record.status}; commenting is closed"
                )

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
            # Expose ONLY the ``reason`` code from metadata (full metadata_json is
            # intentionally omitted) so the inbox can explain why a memory still
            # needs review under auto-save. None for approval types without one.
            "reason": json.loads(record.metadata_json or "{}").get("reason"),
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
