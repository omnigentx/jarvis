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


class ApprovalBlocked(Exception):
    """Raised by ``_execute_agent_turn`` when the approval gate refuses to run.

    Caught by ``_execute_job`` and recorded as ``status="blocked"`` on the
    run row — *not* counted as a failure. This is the difference between
    "user said no / didn't respond in time" (no fail-count bump,
    no auto-disable) and "code crashed" (counts toward 5-strike disable).
    """

    def __init__(self, reason: str, notify: bool = True):
        super().__init__(reason)
        self.reason = reason
        # When False, _execute_job records the blocked run but does NOT push a
        # notification. Used for "awaiting approval" skips: the approval card
        # already notified the user once at creation; re-notifying on every
        # tick while they haven't approved yet is just inbox spam.
        self.notify = notify


class CronScheduler:
    """Core scheduler engine — sleep-until-next pattern."""

    def __init__(self):
        self._running = False
        self._wake_event: asyncio.Event | None = None  # Created in start()
        self._sse_broadcast = None  # set by server.py
        self._agent_app = None  # set by server.py
        self._session_service = None  # set by server.py
        # Track in-flight per job so a long-running run (eg awaiting human
        # approval) doesn't get re-spawned on its next scheduled fire. The
        # scheduler loop itself stays non-blocking — each fire spawns a
        # task and immediately returns to evaluating the next due job.
        self._inflight_tasks: dict[str, asyncio.Task] = {}

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

                # Spawn the job execution as a background task so a blocking
                # call (eg awaiting human approval, which can take an hour by
                # design) doesn't freeze the rest of the schedule. The loop
                # immediately re-evaluates the next due job. Same-job double-
                # fires are prevented by `_inflight_tasks`.
                job_id = next_job.id
                if job_id in self._inflight_tasks and not self._inflight_tasks[job_id].done():
                    logger.info(
                        "[CRON] Job %s still in flight (likely awaiting approval) — "
                        "skipping this fire", job_id,
                    )
                    # MUST yield here too — without an `await` between this
                    # `continue` and the next `_get_next_due()` call we'd hit
                    # a 100% CPU spin (overdue job + sleep_seconds==0 + no
                    # other await on the path). `_get_next_due` itself filters
                    # in-flight ids so it won't keep returning the same job,
                    # but the yield is the safety net.
                    await asyncio.sleep(0)
                else:
                    task = asyncio.create_task(self._run_job_isolated(job_id))
                    self._inflight_tasks[job_id] = task
                    task.add_done_callback(
                        lambda _t, jid=job_id: self._inflight_tasks.pop(jid, None)
                    )
                    # Yield so the freshly spawned task actually runs (and
                    # flips its job's status to "running" inside _execute_job)
                    # before the next iteration's _get_next_due. Without
                    # this, `create_task` only *schedules* the coroutine and
                    # the next loop sees the same job in status="active" with
                    # next_run_at<=now — busy-spin instead of executing.
                    await asyncio.sleep(0)

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

    async def _run_job_isolated(self, job_id: str) -> None:
        """Open a fresh DB session and run one job to completion.

        Used by ``start()`` to dispatch each fire as its own task so the
        scheduler loop stays non-blocking. The session opened here is
        owned by this task — ``_execute_job`` does not share state with
        the scheduler loop's own DB lifetimes.
        """
        db = get_db_session()
        try:
            job = (
                db.query(CronJobModel)
                .filter(
                    CronJobModel.id == job_id,
                    CronJobModel.status.in_(("active", "running")),
                )
                .first()
            )
            if job:
                await self._execute_job(job, db)
        except Exception:
            # _execute_job already handles its own errors and persists them
            # to the run row. Anything that escapes here is a bug in the
            # error-handling path itself — log loudly but don't crash the
            # scheduler.
            logger.error(
                "[CRON] Isolated run for job %s crashed in dispatcher",
                job_id, exc_info=True,
            )
        finally:
            db.close()

    def _get_next_due(self) -> tuple[Optional[CronJobModel], float]:
        """Find the next job to execute and how long to sleep.

        Excludes any job currently in :attr:`_inflight_tasks` — those have
        already been dispatched as a background task (eg waiting on
        human approval) and must not be picked up again until they
        resolve, even though their DB row may still read ``status="active"``
        for the brief window between dispatch and ``_execute_job``'s status
        flip. Without this filter the loop tight-spins on an overdue job
        whose task is queued but not yet scheduled.
        """
        db = get_db_session()
        try:
            q = db.query(CronJobModel).filter(
                CronJobModel.status == "active",
                CronJobModel.next_run_at.isnot(None),
            )
            inflight_ids = [jid for jid, t in self._inflight_tasks.items() if not t.done()]
            if inflight_ids:
                q = q.filter(~CronJobModel.id.in_(inflight_ids))
            job = q.order_by(CronJobModel.next_run_at.asc()).first()
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

        except ApprovalBlocked as exc:
            # Gate refused. NOT a failure — the user simply hasn't approved
            # this payload yet (or rejected it). Never bumps fail_count; the
            # job stays `active` for its next scheduled fire.
            completed_at = time.time()
            duration_ms = int((completed_at - started_at) * 1000)
            block_reason = exc.reason[:500]
            silent = not getattr(exc, "notify", True)

            job.last_run_at = completed_at
            job.last_result = "blocked"
            job.last_error = block_reason
            job.status = "active"  # Restore from 'running'
            if not job.one_shot:
                job.next_run_at = self.compute_next_run(
                    job.schedule_cron, job.calendar_type, job.schedule_timezone, job.lunar_leap
                )

            if silent:
                # "Awaiting approval" skip (notify=False): a RECURRING no-op,
                # not an event. Drop the run row we optimistically opened and
                # skip the run_count bump — otherwise a never-approved `*/5`
                # job accrues ~288 blocked rows/day, unbounded, until it's
                # approved/rejected/deleted. The pending badge
                # (job.approval_status) + the creation-time approval card are
                # the user's signal; nothing per-tick is worth persisting or
                # broadcasting.
                if run_id:
                    stale = db.query(CronRunModel).filter(CronRunModel.id == run_id).first()
                    if stale:
                        db.delete(stale)
                job.updated_at = time.time()
                db.commit()
                logger.info("[CRON] Job '%s' awaiting approval — skipped (no run recorded)", job.name)
                return

            # Genuine one-off block (notify=True): persist the blocked run and
            # notify so the dashboard shows why the job didn't run. run_count
            # counts this attempt so the dashboard reflects that it ticked.
            if run_id:
                run = db.query(CronRunModel).filter(CronRunModel.id == run_id).first()
                if run:
                    run.status = "blocked"
                    run.completed_at = completed_at
                    run.duration_ms = duration_ms
                    run.error = f"approval gate: {block_reason}"
            job.run_count = (job.run_count or 0) + 1
            job.updated_at = time.time()
            db.commit()

            self._broadcast_event("job_blocked", {
                "job_id": job.id,
                "job_name": job.name,
                "run_id": run_id,
                "reason": block_reason,
            })

            # Blocked notification — distinct from `error` so the dashboard
            # can colour-code it (amber) vs failed (red).
            notif = NotificationModel(
                run_id=run_id,
                job_id=job.id,
                type="blocked",
                title=job.name,
                preview=f"Blocked: {block_reason[:180]}",
                content=f"## Job blocked: {job.name}\n\n```\n{block_reason}\n```",
                content_type="markdown",
                is_read=0,
                created_at=completed_at,
                metadata_json=json.dumps({
                    "agent": job.exec_agent or "system",
                    "exec_mode": job.exec_mode,
                    "duration_ms": duration_ms,
                    "status": "blocked",
                }),
            )
            db.add(notif)
            db.commit()
            db.refresh(notif)

            self._broadcast_event("new_notification", {
                "id": notif.id,
                "notif_type": "blocked",
                "title": job.name,
                "preview": f"Blocked: {block_reason[:150]}",
                "created_at": completed_at,
            })

            logger.warning("[CRON] Job '%s' blocked: %s", job.name, block_reason)
            return

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
        """Execute agent turn — send message to specified agent via session.

        Human-approval gate (READ-ONLY here): cron payloads are LLM-supplied
        (the agent calls ``cron_create(exec_payload=...)``) and run unsupervised
        with the configured agent's full tool set — including terminal exec. The
        decision to allow this is made ONCE at CREATION time and stored on
        ``job.approval_status`` (the single source of truth). At fire time we
        only READ that flag — we never block waiting for a human, so an absent
        user can't leave the scheduler hung. A not-yet-approved job simply skips
        this fire and retries on the next tick; the Approvals resolve hook flips
        the flag the moment the user decides.
        """
        if not self._agent_app or not self._session_service:
            raise RuntimeError("Agent references not set — cannot execute agent_turn")

        agent_name = job.exec_agent or "Jarvis"
        payload = job.exec_payload

        logger.info("[CRON] Agent turn: '%s' → agent=%s payload='%s'", job.name, agent_name, payload[:80])

        # Read the approval requirement fresh so the Settings toggle
        # (scheduler.REQUIRE_APPROVAL) hot-reloads without a restart. Fail
        # SAFE: if the config can't be read, require approval.
        try:
            from services.config_service import config_service
            require_approval = str(
                config_service.get("scheduler", "REQUIRE_APPROVAL", default="true")
            ).strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            require_approval = True
        if require_approval and job.approval_status != "approved":
            reason = (
                "awaiting your approval — not yet approved"
                if job.approval_status == "pending"
                else f"approval was {job.approval_status}"
            )
            logger.info("[CRON] Job %s skipped: %s", job.id, reason)
            # notify=False: don't spam the inbox every tick. The approval card
            # created at job-creation time is the user's actionable surface.
            raise ApprovalBlocked(reason, notify=False)

        # Tag every LLM call this turn makes with a deterministic run_id
        # so the dashboard can correlate ``token_usage`` rows back to the
        # cron run. The always-on token-persistence hook (attached at app
        # startup) reads this ContextVar at call time. Without setting
        # it, rows would still be written but un-correlated.
        from services.sse_progress import current_run_id
        run_id = f"cron-{job.id}-{uuid.uuid4().hex[:8]}"
        _run_token = current_run_id.set(run_id)

        try:
            # ``agent_name`` MUST flow through to resume_and_send. Without
            # it the call defaults to Jarvis and the configured target
            # agent (e.g. ResearchAgent for a "Daily AI news summary"
            # job) never runs — the notification UI still shows the
            # configured ``exec_agent`` from job metadata, masking the
            # mismatch. This is why a 45-minute success-marked run came
            # back with an empty body: Jarvis was the one running the
            # prompt and gave up.
            response, session_id = await self._session_service.resume_and_send(
                self._agent_app,
                payload,
                session_id=None,  # New session each run
                agent_name=agent_name,
            )
            result = str(response) if response else "No response"
            if not response:
                # Loud-log empty so the next investigation has evidence
                # without needing to re-run the whole job.
                logger.warning(
                    "[CRON] Agent turn '%s' (agent=%s) returned EMPTY response "
                    "after session=%s — check agent's max-iter / tool-loop "
                    "behaviour and whether it actually exists in agent_app",
                    job.name, agent_name, session_id,
                )

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
        finally:
            current_run_id.reset(_run_token)

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
        Execute each at most once (catch-up policy).

        Dispatches each missed run through :meth:`_run_job_isolated` —
        same pattern as the steady-state loop — so a single missed
        ``agent_turn`` waiting on human approval doesn't freeze the
        rest of the catch-up batch (each gets its own DB session and
        its own approval window).
        """
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
            missed_ids = [j.id for j in missed_jobs]
        finally:
            db.close()

        if not missed_ids:
            return

        logger.info("[CRON] Catching up %d missed jobs", len(missed_ids))
        for job_id in missed_ids:
            if job_id in self._inflight_tasks and not self._inflight_tasks[job_id].done():
                logger.info("[CRON] Catch-up: %s already in flight, skipping", job_id)
                continue
            task = asyncio.create_task(self._run_job_isolated(job_id))
            self._inflight_tasks[job_id] = task
            task.add_done_callback(
                lambda _t, jid=job_id: self._inflight_tasks.pop(jid, None)
            )
        # Yield once so all the spawned tasks at least register status="running"
        # before the steady-state loop picks them up.
        await asyncio.sleep(0)

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
