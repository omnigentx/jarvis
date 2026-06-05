"""
Cron MCP Tool Server — 4 tools for AI-driven cron job management.

Tools:
  1. cron_create — Create a new cron job
  2. cron_list   — List/filter cron jobs
  3. cron_update — Update an existing job (incl. pause/resume)
  4. cron_delete — Delete a job

Runs as a standalone MCP server subprocess (registered in fastagent.config.yaml).
"""
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from croniter import croniter
from lunar_python import Lunar, Solar

from core.database import get_db_session, CronJobModel, CronRunModel

mcp = FastMCP("CronService")


def _validate_cron_expr(cron_expr: str) -> str | None:
    """Validate a 5-field cron expression. Returns error message or None."""
    try:
        croniter(cron_expr)
        return None
    except (ValueError, KeyError) as e:
        return f"Invalid cron expression '{cron_expr}': {e}"


def _compute_next_run(cron_expr: str, calendar_type: str, tz_name: str, lunar_leap: bool = False) -> float:
    """Compute next run timestamp. Imported logic from CronScheduler."""
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    if calendar_type == "solar":
        cron = croniter(cron_expr, now)
        return cron.get_next(datetime).timestamp()
    elif calendar_type == "lunar":
        from datetime import timedelta

        parts = cron_expr.split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression: {cron_expr}")

        minute, hour, day_str, month_str, dow = parts

        if day_str == "*" and month_str == "*":
            cron = croniter(cron_expr, now)
            return cron.get_next(datetime).timestamp()

        target_day = None if day_str == "*" else int(day_str)
        target_month = None if month_str == "*" else int(month_str)

        for offset_days in range(0, 400):
            check_date = now + timedelta(days=offset_days)
            check_solar = Solar.fromYmd(check_date.year, check_date.month, check_date.day)
            check_lunar = check_solar.getLunar()

            day_match = target_day is None or check_lunar.getDay() == target_day
            month_match = target_month is None or check_lunar.getMonth() == target_month

            if day_match and month_match:
                h = int(hour) if hour != "*" else 0
                m = int(minute) if minute != "*" else 0
                candidate = check_date.replace(hour=h, minute=m, second=0, microsecond=0)
                if not candidate.tzinfo:
                    candidate = candidate.replace(tzinfo=tz)
                if candidate.timestamp() > now.timestamp():
                    return candidate.timestamp()

        return (now + timedelta(days=365)).timestamp()
    else:
        raise ValueError(f"Unknown calendar_type: {calendar_type}")


def _require_approval() -> bool:
    """Whether agent-created agent_turn jobs need human approval before they
    may fire. Default ON. The Settings toggle ``scheduler.REQUIRE_APPROVAL``
    can disable it (use at your own risk). Fail safe: if the toggle can't be
    read, require approval.
    """
    try:
        from services.config_service import config_service
        v = config_service.get("scheduler", "REQUIRE_APPROVAL", default="true")
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return True


def _approval_content(job: CronJobModel) -> str:
    """Markdown shown in the approval card for an agent-created job."""
    return (
        f"## Agent-created schedule: `{job.name}`\n\n"
        f"**Schedule:** `{job.schedule_cron}` ({job.calendar_type})\n"
        f"**Target agent:** `{job.exec_agent}`\n\n"
        f"**Payload that will run:**\n\n```text\n{(job.exec_payload or '').rstrip()}\n```\n\n"
        f"---\n\n"
        f"⚠️ **Use at your own risk.** Approving lets this exact payload run on "
        f"the schedule above, unsupervised, with the target agent's full tool "
        f"set — including anything that touches the filesystem, shell, network, "
        f"or external accounts.\n\n"
        f"Approve only if you recognise the payload and trust the source that "
        f"created this job. (This job was created by an agent, not from the "
        f"dashboard — if you didn't ask for it, reject it.)"
    )


def _create_cron_approval_card(*, job_id: str, name: str, exec_agent: str, content_md: str) -> None:
    """Create the user-facing approval card for an agent-created job, via the
    backend RPC socket. Best-effort: the job row is already ``pending`` so it
    can't fire even if the card fails to materialise (fail-safe). ``pause`` is
    False — this is a deferred gate, not a hold on a live agent turn, so the
    agent the user may be chatting with must NOT be frozen.
    """
    from tools.runtime_rpc_client import RuntimeRpcError, call as rpc_call
    try:
        rpc_call("approval.create", {
            "agent_name": exec_agent or "Jarvis",
            "approval_type": "cron_approval",
            "title": f"Schedule approval: {name}",
            "content": content_md,
            "content_format": "markdown",
            "urgency": "normal",
            "pause": False,
            "metadata": {"job_id": job_id},
        })
    except RuntimeRpcError as exc:
        print(f"[cron_create] approval card RPC failed (job stays pending): {exc}",
              file=sys.stderr)


def _format_job(job: CronJobModel) -> dict:
    """Format a job for display."""
    tz = ZoneInfo(job.schedule_timezone or "Asia/Ho_Chi_Minh")
    return {
        "id": job.id,
        "name": job.name,
        "schedule_cron": job.schedule_cron,
        "calendar_type": job.calendar_type,
        "one_shot": job.one_shot,
        "exec_mode": job.exec_mode,
        "exec_payload": job.exec_payload[:100] if job.exec_payload else "",
        "exec_agent": job.exec_agent,
        "status": job.status,
        "approval_status": job.approval_status,
        "last_result": job.last_result,
        "run_count": job.run_count or 0,
        "fail_count": job.fail_count or 0,
        "next_run_at": (
            datetime.fromtimestamp(job.next_run_at, tz=tz).strftime("%Y-%m-%d %H:%M %Z")
            if job.next_run_at else None
        ),
        "last_run_at": (
            datetime.fromtimestamp(job.last_run_at, tz=tz).strftime("%Y-%m-%d %H:%M %Z")
            if job.last_run_at else None
        ),
    }


@mcp.tool()
def cron_create(
    name: str,
    cron_expr: str,
    exec_mode: str,
    exec_payload: str,
    calendar_type: str = "solar",
    one_shot: bool = False,
    exec_agent: str = None,
) -> str:
    """Create a new scheduled job.

    Cron expression: minute hour day_of_month month day_of_week
    - solar: day/month follow the Gregorian calendar (default)
    - lunar: day/month follow the lunar calendar (server converts)
    - one_shot=true: run once then auto-complete
    - exec_agent: which agent to invoke (required when exec_mode='agent_turn')
    - exec_payload: the prompt sent to the agent at trigger time.
      ⚠️ MUST be a direct action prompt; do NOT include the user's scheduling text.
      ❌ WRONG: "Every day at 7am, check the weather in Hanoi"
      ✅ RIGHT: "Check today's weather in Gia Lam, Hanoi and send a notification"

    Examples:
    - "0 9 * * 1-5" → 9am Monday through Friday
    - "0 7 1 * *" + calendar_type="lunar" → 1st of each lunar month
    - "0 15 31 3 *" + one_shot=true → once at 3pm on March 31
    """
    # Validate
    error = _validate_cron_expr(cron_expr)
    if error:
        return f"❌ Error: {error}"

    if exec_mode not in ("reminder", "agent_turn"):
        return "❌ Error: exec_mode must be 'reminder' or 'agent_turn'"

    if calendar_type not in ("solar", "lunar"):
        return "❌ Error: calendar_type must be 'solar' or 'lunar'"

    if exec_mode == "agent_turn" and not exec_agent:
        return "❌ Error: exec_agent is required when exec_mode='agent_turn'"

    # Compute next run
    tz_name = "Asia/Ho_Chi_Minh"
    try:
        next_run = _compute_next_run(cron_expr, calendar_type, tz_name)
    except Exception as e:
        return f"❌ next_run computation failed: {e}"

    # Approval gate — decided HERE at creation time, not at fire time.
    # An agent created this job (created_by='agent'); only agent_turn jobs
    # carry execution risk (reminders just broadcast text). When approval is
    # required, the job starts 'pending' and cannot fire until the user
    # resolves the approval card below. This is the prompt-injection defence:
    # a poisoned cron can't run unsupervised before a human vets its payload.
    needs_approval = exec_mode == "agent_turn" and _require_approval()
    approval_status = "pending" if needs_approval else "approved"

    # Create job
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
            created_by="agent",
            approval_status=approval_status,
        )
        db.add(job)
        db.commit()
        # Build the approval-card markdown while the row is live, so we don't
        # touch the ORM object after the session closes.
        card_content = _approval_content(job) if needs_approval else None

        tz = ZoneInfo(tz_name)
        next_str = datetime.fromtimestamp(next_run, tz=tz).strftime("%Y-%m-%d %H:%M %Z")
    except Exception as e:
        db.rollback()
        return f"❌ Failed to create job: {e}"
    finally:
        db.close()

    if needs_approval:
        # After commit (job durably 'pending') so the card always points at a
        # real row, and outside the DB session so an RPC stall can't hold it.
        _create_cron_approval_card(
            job_id=job_id, name=name, exec_agent=exec_agent, content_md=card_content,
        )
        return (
            f"✅ Job created — ⏳ AWAITING YOUR APPROVAL before it can run\n"
            f"• ID: {job_id}\n"
            f"• Name: {name}\n"
            f"• Cron: {cron_expr} ({calendar_type})\n"
            f"• Mode: {exec_mode}\n"
            f"• One-shot: {'yes' if one_shot else 'no'}\n"
            f"• Next run: {next_str} (will be SKIPPED until approved)\n"
            f"\n⚠️ This job will NOT run until you approve it in the Approvals "
            f"page. Tell the user to review and approve it."
        )

    return (
        f"✅ Job created\n"
        f"• ID: {job_id}\n"
        f"• Name: {name}\n"
        f"• Cron: {cron_expr} ({calendar_type})\n"
        f"• Mode: {exec_mode}\n"
        f"• One-shot: {'yes' if one_shot else 'no'}\n"
        f"• Next run: {next_str}"
    )


@mcp.tool()
def cron_list(
    status: str = "active",
    calendar_type: str = None,
) -> str:
    """List scheduled jobs. Returns each job's full details.

    Parameters:
    - status: 'active' | 'paused' | 'all' | 'disabled' | 'completed'
    - calendar_type: 'solar' | 'lunar' | None (all)
    """
    db = get_db_session()
    try:
        query = db.query(CronJobModel)

        if status != "all":
            query = query.filter(CronJobModel.status == status)

        if calendar_type:
            query = query.filter(CronJobModel.calendar_type == calendar_type)

        jobs = query.order_by(CronJobModel.next_run_at.asc()).all()

        if not jobs:
            return f"📋 No jobs found (status={status}, calendar={calendar_type or 'all'})"

        lines = [f"📋 Jobs ({len(jobs)} found):"]
        for job in jobs:
            f = _format_job(job)
            status_emoji = {"active": "🟢", "paused": "⏸️", "disabled": "🔴", "completed": "✅"}.get(
                f["status"], "❓"
            )
            lines.append(
                f"\n{status_emoji} [{f['id']}] {f['name']}\n"
                f"  Cron: {f['schedule_cron']} ({f['calendar_type']})\n"
                f"  Mode: {f['exec_mode']} | Runs: {f['run_count']} | Fails: {f['fail_count']}\n"
                f"  Next: {f['next_run_at'] or 'N/A'} | Last: {f['last_run_at'] or 'N/A'}"
            )

        return "\n".join(lines)
    finally:
        db.close()


@mcp.tool()
def cron_update(
    job_id: str,
    name: str = None,
    cron_expr: str = None,
    exec_payload: str = None,
    calendar_type: str = None,
    one_shot: bool = None,
    status: str = None,
    exec_agent: str = None,
) -> str:
    """Update a scheduled job. Only pass the fields that should change.
    Set status='paused' to pause, 'active' to resume.

    Parameters:
    - job_id: target job id (required)
    - name: new name
    - cron_expr: new cron expression
    - exec_payload: new payload
    - calendar_type: 'solar' | 'lunar'
    - one_shot: true/false
    - status: 'active' | 'paused'
    - exec_agent: new agent name
    """
    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        if not job:
            return f"❌ Job not found: {job_id}"

        changes = []
        payload_changed = False

        if name is not None:
            job.name = name
            changes.append(f"name → {name}")

        if exec_payload is not None and exec_payload != job.exec_payload:
            job.exec_payload = exec_payload
            payload_changed = True
            changes.append(f"payload updated")

        if exec_agent is not None:
            job.exec_agent = exec_agent
            changes.append(f"exec_agent → {exec_agent}")

        if calendar_type is not None:
            if calendar_type not in ("solar", "lunar"):
                return "❌ calendar_type must be 'solar' or 'lunar'"
            job.calendar_type = calendar_type
            changes.append(f"calendar → {calendar_type}")

        if one_shot is not None:
            job.one_shot = one_shot
            changes.append(f"one_shot → {one_shot}")

        if status is not None:
            if status not in ("active", "paused"):
                return "❌ status must be 'active' or 'paused'"
            old_status = job.status
            job.status = status
            if status == "active" and old_status in ("paused", "disabled"):
                job.fail_count = 0  # Reset on re-enable
            changes.append(f"status: {old_status} → {status}")

        if cron_expr is not None:
            error = _validate_cron_expr(cron_expr)
            if error:
                return f"❌ {error}"
            job.schedule_cron = cron_expr
            changes.append(f"cron → {cron_expr}")

        # Recompute next_run_at if schedule changed
        if cron_expr is not None or calendar_type is not None or status == "active":
            try:
                job.next_run_at = _compute_next_run(
                    job.schedule_cron,
                    job.calendar_type,
                    job.schedule_timezone,
                    job.lunar_leap,
                )
            except Exception as e:
                return f"❌ next_run computation failed: {e}"

        # An agent editing the payload of an agent_turn job invalidates any
        # prior approval — the bytes that were vetted no longer match what
        # will run. Re-gate it. This closes the "create benign job → get it
        # approved → quietly swap in a malicious payload" path without needing
        # to hash anything: the approval flag is the state, reset it on edit.
        reapproval_needed = (
            payload_changed
            and job.exec_mode == "agent_turn"
            and _require_approval()
        )
        if reapproval_needed:
            job.approval_status = "pending"
            changes.append("approval reset → pending (payload changed)")

        job.updated_at = time.time()
        # Snapshot fields for the post-commit approval card.
        card_content = _approval_content(job) if reapproval_needed else None
        card_name, card_agent = job.name, job.exec_agent
        db.commit()

        if not changes:
            return f"ℹ️ No changes for job [{job_id}]"

        if reapproval_needed:
            _create_cron_approval_card(
                job_id=job_id, name=card_name, exec_agent=card_agent, content_md=card_content,
            )
            changes.append("⏳ job will NOT run until you re-approve it")

        return f"✅ Updated job [{job_id}] {job.name}:\n• " + "\n• ".join(changes)
    except Exception as e:
        db.rollback()
        return f"❌ Update failed: {e}"
    finally:
        db.close()


@mcp.tool()
def cron_delete(
    job_id: str,
) -> str:
    """Permanently delete a scheduled job.

    Parameters:
    - job_id: target job id (required)
    """
    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        if not job:
            return f"❌ Job not found: {job_id}"

        job_name = job.name

        # Delete associated runs
        db.query(CronRunModel).filter(CronRunModel.job_id == job_id).delete()
        db.delete(job)
        db.commit()

        return f"✅ Deleted job [{job_id}] '{job_name}' and all run history."
    except Exception as e:
        db.rollback()
        return f"❌ Delete failed: {e}"
    finally:
        db.close()


if __name__ == "__main__":
    mcp.run()
