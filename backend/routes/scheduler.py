"""
Scheduler REST API — dashboard endpoints + SSE stream.

Endpoints:
  GET  /api/scheduler/stats         — Metric cards
  GET  /api/scheduler/jobs          — List all jobs
  POST /api/scheduler/jobs          — Create job (from dashboard)
  PUT  /api/scheduler/jobs/:id      — Update job
  DEL  /api/scheduler/jobs/:id      — Delete job
  POST /api/scheduler/jobs/:id/pause  — Pause job
  POST /api/scheduler/jobs/:id/resume — Resume job
  POST /api/scheduler/jobs/:id/retry  — Retry last failed run
  GET  /api/scheduler/runs          — Execution history
  GET  /api/scheduler/presets       — Holiday presets
  GET  /api/scheduler/stream        — SSE realtime events
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from core.auth import verify_api_key
from core.database import get_db_session, CronJobModel, CronRunModel

logger = logging.getLogger("scheduler_api")

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


def _format_job_api(job: CronJobModel) -> dict:
    """Format job for API response."""
    tz = ZoneInfo(job.schedule_timezone or "Asia/Ho_Chi_Minh")
    return {
        "id": job.id,
        "name": job.name,
        "schedule_cron": job.schedule_cron,
        "calendar_type": job.calendar_type,
        "one_shot": bool(job.one_shot),
        "schedule_timezone": job.schedule_timezone,
        "exec_mode": job.exec_mode,
        "exec_payload": job.exec_payload,
        "exec_agent": job.exec_agent,
        "status": job.status,
        "last_run_at": job.last_run_at,
        "last_result": job.last_result,
        "last_error": job.last_error,
        "next_run_at": job.next_run_at,
        "next_run_display": (
            datetime.fromtimestamp(job.next_run_at, tz=tz).strftime("%Y-%m-%d %H:%M")
            if job.next_run_at else None
        ),
        "run_count": job.run_count or 0,
        "fail_count": job.fail_count or 0,
        "total_fail": job.total_fail or 0,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "created_by": job.created_by,
        "approval_status": job.approval_status,
    }


def _format_run_api(run: CronRunModel) -> dict:
    """Format run for API response."""
    return {
        "id": run.id,
        "job_id": run.job_id,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "status": run.status,
        "duration_ms": run.duration_ms,
        "error": run.error,
        "result_type": run.result_type,
        "result_json": json.loads(run.result_json) if run.result_json else None,
        "attempt": run.attempt,
    }


# ─── Stats ────────────────────────────────────────────

@router.get("/stats")
async def get_stats(_auth: bool = Depends(verify_api_key)):
    """Get scheduler dashboard stats."""
    from services.cron_scheduler import CronScheduler
    return CronScheduler.get_stats()


# ─── Jobs CRUD ────────────────────────────────────────

@router.get("/jobs")
async def list_jobs(
    status: str = Query("all"),
    calendar_type: str = Query(None),
    _auth: bool = Depends(verify_api_key),
):
    """List all cron jobs."""
    db = get_db_session()
    try:
        query = db.query(CronJobModel)

        if status != "all":
            query = query.filter(CronJobModel.status == status)
        if calendar_type:
            query = query.filter(CronJobModel.calendar_type == calendar_type)

        jobs = query.order_by(CronJobModel.created_at.desc()).all()
        return {"jobs": [_format_job_api(j) for j in jobs], "total": len(jobs)}
    finally:
        db.close()


@router.post("/jobs")
async def create_job(request: Request, _auth: bool = Depends(verify_api_key)):
    """Create a cron job from dashboard."""
    from services.cron_scheduler import cron_scheduler
    from croniter import croniter

    body = await request.json()

    name = body.get("name", "").strip()
    cron_expr = body.get("cron_expr", "").strip()
    exec_mode = body.get("exec_mode", "reminder")
    exec_payload = body.get("exec_payload", "").strip()
    calendar_type = body.get("calendar_type", "solar")
    one_shot = body.get("one_shot", False)
    exec_agent = body.get("exec_agent")

    if not name or not cron_expr or not exec_payload:
        return {"error": "name, cron_expr, exec_payload are required"}, 400

    try:
        croniter(cron_expr)
    except (ValueError, KeyError) as e:
        return {"error": f"Invalid cron expression: {e}"}

    tz_name = "Asia/Ho_Chi_Minh"
    next_run = cron_scheduler.compute_next_run(cron_expr, calendar_type, tz_name)

    job_id = str(uuid.uuid4())[:8]
    now = time.time()

    db = get_db_session()
    try:
        job = CronJobModel(
            id=job_id,
            user_id="default",
            name=name,
            schedule_cron=cron_expr,
            calendar_type=calendar_type,
            one_shot=one_shot,
            schedule_timezone=tz_name,
            exec_mode=exec_mode,
            exec_payload=exec_payload,
            exec_agent=exec_agent,
            status="active",
            next_run_at=next_run,
            created_at=now,
            updated_at=now,
            created_by="user",
        )
        db.add(job)
        db.commit()

        # Wake scheduler to recalculate next
        cron_scheduler.wake()

        return {"job": _format_job_api(job)}
    except Exception as e:
        db.rollback()
        return {"error": str(e)}
    finally:
        db.close()


@router.put("/jobs/{job_id}")
async def update_job(job_id: str, request: Request, _auth: bool = Depends(verify_api_key)):
    """Update a cron job."""
    from services.cron_scheduler import cron_scheduler
    from croniter import croniter

    body = await request.json()
    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        if not job:
            return {"error": f"Job {job_id} not found"}

        for field in ("name", "exec_payload", "exec_agent", "calendar_type", "exec_mode"):
            if field in body and body[field] is not None:
                setattr(job, field, body[field])

        # Dashboard edits are user-driven and trusted (the user is present and
        # in control). If this job is awaiting agent-approval, the user editing
        # it HERE is the vetting step → mark it approved so it can run. We also
        # resolve any stale pending approval card below (after commit) so a
        # later click on it can't approve a payload the user never saw — the
        # SSoT (job.approval_status) and the inbox card stay consistent.
        #
        # A `rejected` job is deliberately NOT auto-approved here: rejection is
        # an explicit "no", and a trivial dashboard edit (e.g. a rename) must
        # not silently revive it. Reviving a rejected job takes an explicit
        # approve action, not an incidental field change.
        was_pending = job.approval_status == "pending"
        if was_pending:
            job.approval_status = "approved"

        if "one_shot" in body:
            job.one_shot = bool(body["one_shot"])

        if "cron_expr" in body:
            try:
                croniter(body["cron_expr"])
            except (ValueError, KeyError) as e:
                return {"error": f"Invalid cron: {e}"}
            job.schedule_cron = body["cron_expr"]

        if "status" in body:
            if body["status"] in ("active", "paused"):
                job.status = body["status"]
                if body["status"] == "active":
                    job.fail_count = 0

        # Recompute next_run
        if job.status == "active":
            job.next_run_at = cron_scheduler.compute_next_run(
                job.schedule_cron, job.calendar_type, job.schedule_timezone, job.lunar_leap
            )

        job.updated_at = time.time()
        db.commit()
        job_api = _format_job_api(job)
    except Exception as e:
        db.rollback()
        return {"error": str(e)}
    finally:
        db.close()

    # After commit + close so the resolve's own sessions don't nest in ours.
    if was_pending:
        _approve_pending_cron_cards(job_id)

    cron_scheduler.wake()
    return {"job": job_api}


def _approve_pending_cron_cards(job_id: str) -> None:
    """Resolve (as approved) any pending cron_approval card pointing at this
    job. Used when the user edits the job on the dashboard — a trusted action
    that supersedes the agent-created approval request. Best-effort: a missing
    card just means there's nothing to clear (the job flag is already the SSoT)."""
    from services.approval_service import approval_service
    from core.database import ApprovalRequestModel

    db = get_db_session()
    try:
        cards = (
            db.query(ApprovalRequestModel)
            .filter(
                ApprovalRequestModel.approval_type == "cron_approval",
                ApprovalRequestModel.status == "pending",
            )
            .all()
        )
        ids = [
            c.id for c in cards
            if json.loads(c.metadata_json or "{}").get("job_id") == job_id
        ]
    finally:
        db.close()

    for cid in ids:
        try:
            approval_service.resolve_approval(
                cid, decision="approve", comment="approved via dashboard edit",
            )
        except Exception as exc:
            logger.warning("[scheduler] could not resolve cron card %s: %s", cid, exc)


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str, _auth: bool = Depends(verify_api_key)):
    """Delete a cron job and its history."""
    from services.cron_scheduler import cron_scheduler

    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        if not job:
            return {"error": f"Job {job_id} not found"}

        db.query(CronRunModel).filter(CronRunModel.job_id == job_id).delete()
        db.delete(job)
        db.commit()

        cron_scheduler.wake()
        return {"deleted": job_id}
    except Exception as e:
        db.rollback()
        return {"error": str(e)}
    finally:
        db.close()


# ─── Quick Actions ────────────────────────────────────

@router.post("/jobs/{job_id}/pause")
async def pause_job(job_id: str, _auth: bool = Depends(verify_api_key)):
    """Pause a job."""
    from services.cron_scheduler import cron_scheduler

    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        if not job:
            return {"error": f"Job {job_id} not found"}
        job.status = "paused"
        job.updated_at = time.time()
        db.commit()
        cron_scheduler.wake()
        return {"job": _format_job_api(job)}
    finally:
        db.close()


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str, _auth: bool = Depends(verify_api_key)):
    """Resume a paused/disabled job."""
    from services.cron_scheduler import cron_scheduler

    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        if not job:
            return {"error": f"Job {job_id} not found"}
        job.status = "active"
        job.fail_count = 0
        job.next_run_at = cron_scheduler.compute_next_run(
            job.schedule_cron, job.calendar_type, job.schedule_timezone, job.lunar_leap
        )
        job.updated_at = time.time()
        db.commit()
        cron_scheduler.wake()
        return {"job": _format_job_api(job)}
    finally:
        db.close()


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, _auth: bool = Depends(verify_api_key)):
    """Retry a failed job immediately by resetting next_run_at to now."""
    from services.cron_scheduler import cron_scheduler

    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        if not job:
            return {"error": f"Job {job_id} not found"}
        job.status = "active"
        job.next_run_at = time.time()  # Execute ASAP
        job.fail_count = 0
        job.updated_at = time.time()
        db.commit()
        cron_scheduler.wake()
        return {"job": _format_job_api(job), "message": "Job will retry immediately"}
    finally:
        db.close()


# ─── Runs ─────────────────────────────────────────────

@router.get("/runs")
async def list_runs(
    job_id: str = Query(None),
    limit: int = Query(50, ge=1, le=200),
    _auth: bool = Depends(verify_api_key),
):
    """Get execution history."""
    db = get_db_session()
    try:
        query = db.query(CronRunModel)
        if job_id:
            query = query.filter(CronRunModel.job_id == job_id)
        runs = query.order_by(CronRunModel.started_at.desc()).limit(limit).all()

        # Enrich with job names
        job_ids = set(r.job_id for r in runs)
        jobs = {j.id: j.name for j in db.query(CronJobModel).filter(CronJobModel.id.in_(job_ids)).all()} if job_ids else {}

        result = []
        for r in runs:
            d = _format_run_api(r)
            d["job_name"] = jobs.get(r.job_id, "Unknown")
            result.append(d)

        return {"runs": result, "total": len(result)}
    finally:
        db.close()


# ─── Presets ──────────────────────────────────────────

@router.get("/presets")
async def get_presets(_auth: bool = Depends(verify_api_key)):
    """Get holiday presets for quick job creation."""
    preset_path = Path(__file__).parent.parent / "config" / "holiday_presets.yaml"
    if not preset_path.exists():
        return {"solar_holidays": [], "lunar_holidays": []}
    try:
        with open(preset_path) as f:
            presets = yaml.safe_load(f) or {}
        return presets
    except Exception as e:
        logger.warning("Failed to load presets: %s", e)
        return {"solar_holidays": [], "lunar_holidays": []}


# ─── SSE Stream ──────────────────────────────────────

@router.get("/stream")
async def scheduler_stream(
    request: Request,
    _auth: bool = Depends(verify_api_key),
):
    """SSE stream for realtime scheduler events."""

    from services.cron_scheduler import scheduler_stream_manager

    sub_id, queue = scheduler_stream_manager.subscribe()

    async def event_generator():
        try:
            # Send initial stats
            from services.cron_scheduler import CronScheduler
            stats = CronScheduler.get_stats()
            yield f"data: {json.dumps({'type': 'init', 'stats': stats})}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield f": keepalive\n\n"
        finally:
            scheduler_stream_manager.unsubscribe(sub_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
