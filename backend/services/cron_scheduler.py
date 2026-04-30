"""
CronScheduler — Event-driven cron scheduler for Jarvis.

Architecture:
  - sleep-until-next pattern (no polling!)
  - Supports solar + lunar calendar
  - Registers into BackgroundJobScheduler for lifecycle management
  - SSE broadcast for realtime dashboard updates

Phase: All phases (Core + Lunar + Agent Turn + Catch-up)
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from croniter import croniter
from lunar_python import Lunar, Solar

from core.database import get_db_session, CronJobModel, CronRunModel, NotificationModel
from services.background_jobs import BackgroundJobRunner

logger = logging.getLogger("cron_scheduler")


class CronScheduler:
    """Core scheduler engine — sleep-until-next pattern."""

    def __init__(self):
        self._running = False
        self._wake_event: asyncio.Event | None = None  # Created in start()
        self._sse_broadcast = None  # set by server.py
        self._agent_app = None  # set by server.py
        self._session_service = None  # set by server.py

    def set_sse_broadcast(self, broadcast_fn):
        """Set SSE broadcast function for realtime updates."""
        self._sse_broadcast = broadcast_fn

    def set_agent_refs(self, agent_app, session_service):
        """Set agent references for agent_turn execution."""
        self._agent_app = agent_app
        self._session_service = session_service

    def wake(self):
        """Wake the scheduler (called after job create/update/delete)."""
        if self._wake_event:
            self._wake_event.set()

    async def start(self):
        """Main loop — sleep until next job, execute, repeat."""
        self._running = True
        self._wake_event = asyncio.Event()  # Must be created inside running loop
        logger.info("[CRON] Scheduler started")

        # Catch-up: check for missed runs on startup
        await self._catch_up_missed()

        while self._running:
            try:
                next_job, sleep_seconds = self._get_next_due()

                if next_job is None:
                    # No active jobs — sleep until woken
                    logger.debug("[CRON] No active jobs, waiting for wake signal")
                    self._wake_event.clear()
                    await self._wake_event.wait()
                    continue

                if sleep_seconds > 0:
                    logger.debug(
                        "[CRON] Next job '%s' in %.1fs (at %s)",
                        next_job.name,
                        sleep_seconds,
                        datetime.fromtimestamp(next_job.next_run_at).strftime("%H:%M:%S"),
                    )
                    self._wake_event.clear()
                    try:
                        await asyncio.wait_for(
                            self._wake_event.wait(), timeout=sleep_seconds
                        )
                        # Woken early — recalculate
                        continue
                    except asyncio.TimeoutError:
                        # Timer expired — time to execute
                        pass

                # Re-fetch job (it may have been modified/deleted while sleeping)
                db = get_db_session()
                try:
                    job = db.query(CronJobModel).filter(
                        CronJobModel.id == next_job.id,
                        CronJobModel.status == "active",
                    ).first()
                    if job:
                        await self._execute_job(job, db)
                finally:
                    db.close()

            except asyncio.CancelledError:
                logger.info("[CRON] Scheduler shutdown")
                break
            except Exception as e:
                logger.error("[CRON] Loop error: %s", e, exc_info=True)
                await asyncio.sleep(5)

    def stop(self):
        """Stop the scheduler."""
        self._running = False
        self._wake_event.set()
        logger.info("[CRON] Scheduler stopped")

    # ─── Core Logic ───────────────────────────────────────

    def _get_next_due(self) -> tuple[Optional[CronJobModel], float]:
        """Find the next job to execute and how long to sleep."""
        db = get_db_session()
        try:
            job = (
                db.query(CronJobModel)
                .filter(
                    CronJobModel.status == "active",
                    CronJobModel.next_run_at.isnot(None),
                )
                .order_by(CronJobModel.next_run_at.asc())
                .first()
            )
            if not job:
                return None, 0

            now = time.time()
            sleep_seconds = max(0, job.next_run_at - now)
            # Detach from session for use outside
            db.expunge(job)
            return job, sleep_seconds
        finally:
            db.close()

    async def _execute_job(self, job: CronJobModel, db):
        """Execute a single cron job."""
        run_id = None
        started_at = time.time()

        try:
            # Create run record
            run = CronRunModel(
                job_id=job.id,
                started_at=started_at,
                status="running",
            )
            db.add(run)

            # Mark job as running in DB so API reflects realtime status
            prev_status = job.status
            job.status = "running"
            job.updated_at = time.time()
            db.commit()
            db.refresh(run)
            run_id = run.id

            # Broadcast start event
            self._broadcast_event("job_started", {
                "job_id": job.id,
                "job_name": job.name,
                "run_id": run_id,
                "exec_mode": job.exec_mode,
            })

            logger.info("[CRON] Executing job '%s' (mode=%s)", job.name, job.exec_mode)

            # Execute based on mode
            result_text = None
            if job.exec_mode == "reminder":
                result_text = await self._execute_reminder(job)
            elif job.exec_mode == "agent_turn":
                result_text = await self._execute_agent_turn(job)
            else:
                raise ValueError(f"Unknown exec_mode: {job.exec_mode}")

            # Success
            completed_at = time.time()
            duration_ms = int((completed_at - started_at) * 1000)

            run.status = "success"
            run.completed_at = completed_at
            run.duration_ms = duration_ms
            run.result_type = "text"
            run.result_json = json.dumps({"text": result_text[:2000] if result_text else ""})

            job.last_run_at = completed_at
            job.last_result = "success"
            job.last_error = None
            job.run_count = (job.run_count or 0) + 1
            job.fail_count = 0  # Reset consecutive failures

            # Handle one-shot
            if job.one_shot:
                job.status = "completed"
                job.next_run_at = None
            else:
                job.status = "active"  # Restore from 'running'
                job.next_run_at = self.compute_next_run(
                    job.schedule_cron, job.calendar_type, job.schedule_timezone, job.lunar_leap
                )

            job.updated_at = time.time()
            db.commit()

            self._broadcast_event("job_completed", {
                "job_id": job.id,
                "job_name": job.name,
                "run_id": run_id,
                "status": "success",
                "duration_ms": duration_ms,
                "one_shot_completed": job.one_shot,
            })

            # Create notification
            notif_type = "reminder" if job.exec_mode == "reminder" else "agent_result"
            content = result_text or ""
            preview = content[:200].replace("\n", " ").strip() if content else ""
            content_type_val = "markdown" if job.exec_mode == "agent_turn" else "text"
            notif = NotificationModel(
                run_id=run_id,
                job_id=job.id,
                type=notif_type,
                title=job.name,
                preview=preview,
                content=content,
                content_type=content_type_val,
                is_read=0,
                created_at=completed_at,
                metadata_json=json.dumps({
                    "agent": job.exec_agent or "system",
                    "exec_mode": job.exec_mode,
                    "duration_ms": duration_ms,
                    "status": "success",
                }),
            )
            db.add(notif)
            db.commit()
            db.refresh(notif)

            self._broadcast_event("new_notification", {
                "id": notif.id,
                "notif_type": notif_type,
                "title": job.name,
                "preview": preview,
                "created_at": completed_at,
            })

            logger.info(
                "[CRON] Job '%s' completed in %dms (next=%s)",
                job.name,
                duration_ms,
                datetime.fromtimestamp(job.next_run_at).strftime("%Y-%m-%d %H:%M") if job.next_run_at else "none",
            )

        except Exception as e:
            # Failure
            completed_at = time.time()
            duration_ms = int((completed_at - started_at) * 1000)
            error_msg = str(e)

            if run_id:
                run = db.query(CronRunModel).filter(CronRunModel.id == run_id).first()
                if run:
                    run.status = "failed"
                    run.completed_at = completed_at
                    run.duration_ms = duration_ms
                    run.error = error_msg[:1000]

            job.last_run_at = completed_at
            job.last_result = "failed"
            job.last_error = error_msg[:500]
            job.fail_count = (job.fail_count or 0) + 1
            job.total_fail = (job.total_fail or 0) + 1
            job.run_count = (job.run_count or 0) + 1

            # Restore status from 'running'
            job.status = "active"

            # Auto-disable after 5 consecutive failures
            if job.fail_count >= 5:
                job.status = "disabled"
                logger.warning("[CRON] Job '%s' disabled after %d consecutive failures", job.name, job.fail_count)

            # Compute next run even on failure
            if job.status == "active" and not job.one_shot:
                job.next_run_at = self.compute_next_run(
                    job.schedule_cron, job.calendar_type, job.schedule_timezone, job.lunar_leap
                )

            job.updated_at = time.time()
            db.commit()

            self._broadcast_event("job_failed", {
                "job_id": job.id,
                "job_name": job.name,
                "run_id": run_id,
                "error": error_msg[:200],
                "fail_count": job.fail_count,
                "disabled": job.status == "disabled",
            })

            # Create error notification
            notif = NotificationModel(
                run_id=run_id,
                job_id=job.id,
                type="error",
                title=job.name,
                preview=f"Error: {error_msg[:200]}",
                content=f"## Job Failed: {job.name}\n\n```\n{error_msg[:2000]}\n```",
                content_type="markdown",
                is_read=0,
                created_at=completed_at,
                metadata_json=json.dumps({
                    "agent": job.exec_agent or "system",
                    "exec_mode": job.exec_mode,
                    "duration_ms": duration_ms,
                    "status": "failed",
                    "fail_count": job.fail_count,
                }),
            )
            db.add(notif)
            db.commit()
            db.refresh(notif)

            self._broadcast_event("new_notification", {
                "id": notif.id,
                "notif_type": "error",
                "title": job.name,
                "preview": f"Error: {error_msg[:150]}",
                "created_at": completed_at,
            })

            logger.error("[CRON] Job '%s' failed: %s", job.name, error_msg, exc_info=True)

    # ─── Execution Modes ──────────────────────────────────

    async def _execute_reminder(self, job: CronJobModel) -> str:
        """Execute reminder — broadcast SSE notification."""
        msg = job.exec_payload or "Reminder"
        self._broadcast_event("reminder", {
            "job_id": job.id,
            "job_name": job.name,
            "message": msg,
            "calendar_type": job.calendar_type,
        })
        logger.info("[CRON] Reminder: '%s' → %s", job.name, msg[:80])
        return msg

    async def _execute_agent_turn(self, job: CronJobModel) -> str:
        """Execute agent turn — send message to specified agent via session."""
        if not self._agent_app or not self._session_service:
            raise RuntimeError("Agent references not set — cannot execute agent_turn")

        agent_name = job.exec_agent or "Jarvis"
        payload = job.exec_payload

        logger.info("[CRON] Agent turn: '%s' → agent=%s payload='%s'", job.name, agent_name, payload[:80])

        try:
            response, session_id = await self._session_service.resume_and_send(
                self._agent_app,
                payload,
                session_id=None,  # New session each run
            )
            result = str(response) if response else "No response"

            self._broadcast_event("agent_turn_result", {
                "job_id": job.id,
                "job_name": job.name,
                "agent": agent_name,
                "result_preview": result[:200],
                "session_id": session_id,
            })

            return result
        except Exception as e:
            raise RuntimeError(f"Agent turn failed for '{agent_name}': {e}") from e

    # ─── Lunar Calendar ───────────────────────────────────

    @staticmethod
    def compute_next_run(
        cron_expr: str,
        calendar_type: str = "solar",
        tz_name: str = "Asia/Ho_Chi_Minh",
        lunar_leap: bool = False,
    ) -> float:
        """Compute the next run timestamp for a cron expression.

        For solar: standard croniter.
        For lunar: resolve day/month from cron as lunar → convert to solar.
        """
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)

        if calendar_type == "solar":
            return CronScheduler._next_solar(cron_expr, now, tz)
        elif calendar_type == "lunar":
            return CronScheduler._next_lunar(cron_expr, now, tz, lunar_leap)
        else:
            raise ValueError(f"Unknown calendar_type: {calendar_type}")

    @staticmethod
    def _next_solar(cron_expr: str, now: datetime, tz: ZoneInfo) -> float:
        """Standard cron next-run for solar calendar."""
        cron = croniter(cron_expr, now)
        next_dt = cron.get_next(datetime)
        return next_dt.timestamp()

    @staticmethod
    def _next_lunar(cron_expr: str, now: datetime, tz: ZoneInfo, lunar_leap: bool = False) -> float:
        """Compute next run for lunar calendar cron.

        Parse day/month from cron expression as lunar dates.
        Use croniter for minute/hour/dow, then resolve day/month via lunar_python.
        """
        parts = cron_expr.split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression: {cron_expr}")

        minute, hour, day_str, month_str, dow = parts

        # If day and month are wildcards, use standard cron (daily/hourly schedules)
        if day_str == "*" and month_str == "*":
            # Pure time-based schedule, no lunar date involved
            return CronScheduler._next_solar(cron_expr, now, tz)

        # Get current lunar date
        now_solar = Solar.fromYmd(now.year, now.month, now.day)
        now_lunar = now_solar.getLunar()

        # Parse target lunar day/month
        target_day = None if day_str == "*" else int(day_str)
        target_month = None if month_str == "*" else int(month_str)

        # Search up to 400 days ahead for the next matching lunar date
        for offset_days in range(0, 400):
            check_date = now + timedelta(days=offset_days)
            check_solar = Solar.fromYmd(check_date.year, check_date.month, check_date.day)
            check_lunar = check_solar.getLunar()

            lunar_day = check_lunar.getDay()
            lunar_month = check_lunar.getMonth()

            # Check if this date matches the lunar pattern
            day_match = target_day is None or lunar_day == target_day
            month_match = target_month is None or lunar_month == target_month

            if day_match and month_match:
                # Build the full datetime with hour:minute from cron
                h = int(hour) if hour != "*" else 0
                m = int(minute) if minute != "*" else 0

                candidate = check_date.replace(hour=h, minute=m, second=0, microsecond=0)
                if not candidate.tzinfo:
                    candidate = candidate.replace(tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

                if candidate.timestamp() > now.timestamp():
                    return candidate.timestamp()

        # Fallback: 1 year from now
        logger.warning("[CRON] Could not find next lunar date for '%s', fallback +1yr", cron_expr)
        return (now + timedelta(days=365)).timestamp()

    # ─── Catch-up ─────────────────────────────────────────

    async def _catch_up_missed(self):
        """On startup, check for jobs whose next_run_at is in the past.
        Execute each at most once (catch-up policy)."""
        db = get_db_session()
        try:
            now = time.time()
            missed_jobs = (
                db.query(CronJobModel)
                .filter(
                    CronJobModel.status == "active",
                    CronJobModel.next_run_at.isnot(None),
                    CronJobModel.next_run_at < now,
                )
                .all()
            )
            if missed_jobs:
                logger.info("[CRON] Catching up %d missed jobs", len(missed_jobs))
                for job in missed_jobs:
                    try:
                        await self._execute_job(job, db)
                    except Exception as e:
                        logger.error("[CRON] Catch-up failed for '%s': %s", job.name, e)
        finally:
            db.close()

    # ─── SSE Broadcast ────────────────────────────────────

    def _broadcast_event(self, event_type: str, data: dict):
        """Broadcast event to SSE subscribers."""
        if self._sse_broadcast:
            event = {
                "type": event_type,
                "timestamp": time.time(),
                **data,
            }
            try:
                self._sse_broadcast(event)
            except Exception as e:
                logger.debug("[CRON] SSE broadcast error: %s", e)

    # ─── Stats ────────────────────────────────────────────

    @staticmethod
    def get_stats() -> dict:
        """Get dashboard stats from DB."""
        db = get_db_session()
        try:
            now = time.time()
            day_ago = now - 86400

            # Active jobs
            active_count = db.query(CronJobModel).filter(
                CronJobModel.status == "active"
            ).count()

            # Jobs needing attention (disabled or >3 consecutive failures)
            needs_attention = db.query(CronJobModel).filter(
                CronJobModel.status.in_(["disabled"]) |
                (CronJobModel.fail_count >= 3)
            ).count()

            # Runs in last 24h
            recent_runs = db.query(CronRunModel).filter(
                CronRunModel.started_at >= day_ago
            ).all()

            total_runs = len(recent_runs)
            success_runs = sum(1 for r in recent_runs if r.status == "success")
            failed_runs = sum(1 for r in recent_runs if r.status in ("failed", "error"))
            success_rate = (success_runs / total_runs) if total_runs > 0 else 1.0

            # Next 24h upcoming
            next_24h = db.query(CronJobModel).filter(
                CronJobModel.status == "active",
                CronJobModel.next_run_at.isnot(None),
                CronJobModel.next_run_at <= now + 86400,
            ).count()

            return {
                "active_jobs": active_count,
                "needs_attention": needs_attention,
                "success_rate": round(success_rate, 3),
                "next_24h": next_24h,
                "runs_today": total_runs,
                "failed_today": failed_runs,
            }
        finally:
            db.close()


class CronBackgroundJob(BackgroundJobRunner):
    """Wraps CronScheduler as a BackgroundJobRunner for lifecycle management."""

    job_name = "cron_scheduler"

    def __init__(self, scheduler: CronScheduler):
        self.scheduler = scheduler

    async def get_next_task(self) -> dict | None:
        """Not used — CronScheduler manages its own event loop."""
        return None

    async def execute_task(self, task: dict) -> bool:
        """Not used."""
        return True

    def get_status(self) -> dict:
        """Return status for monitoring."""
        stats = CronScheduler.get_stats()
        return {
            "job_name": self.job_name,
            "status": "running" if self.scheduler._running else "idle",
            **stats,
        }


# ─── Scheduler Stream Manager (SSE for dashboard) ────────

class SchedulerStreamManager:
    """SSE subscriber management for scheduler events."""

    def __init__(self):
        self._subscribers: dict[str, asyncio.Queue] = {}
        self._counter = 0

    def subscribe(self) -> tuple[str, asyncio.Queue]:
        """Create a new subscriber."""
        self._counter += 1
        sub_id = f"sched_sub_{self._counter}_{int(time.time())}"
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers[sub_id] = q
        logger.debug("[CRON-SSE] Subscriber added: %s (total=%d)", sub_id, len(self._subscribers))
        return sub_id, q

    def unsubscribe(self, sub_id: str):
        """Remove a subscriber."""
        self._subscribers.pop(sub_id, None)
        logger.debug("[CRON-SSE] Subscriber removed: %s", sub_id)

    def broadcast(self, event: dict):
        """Fan out event to all subscribers."""
        for sub_id, q in list(self._subscribers.items()):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("[CRON-SSE] Queue full for %s, dropping", sub_id)


# Singletons
scheduler_stream_manager = SchedulerStreamManager()
cron_scheduler = CronScheduler()
cron_scheduler.set_sse_broadcast(scheduler_stream_manager.broadcast)
