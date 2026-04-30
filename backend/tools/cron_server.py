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
    """Tạo một tác vụ định kỳ mới.

    Cron expression: minute hour day_of_month month day_of_week
    - solar: day/month là dương lịch (mặc định)
    - lunar: day/month là âm lịch (server tự convert)
    - one_shot=true: chạy 1 lần rồi auto-complete
    - exec_agent: chỉ định agent thực hiện (bắt buộc khi exec_mode='agent_turn')
    - exec_payload: nội dung sẽ gửi cho agent khi cron trigger.
      ⚠️ PHẢI là prompt hành động trực tiếp, KHÔNG copy phần lịch/scheduling của user.
      ❌ SAI: "Mỗi ngày lúc 7h sáng, hãy kiểm tra thời tiết tại HN"
      ✅ ĐÚNG: "Kiểm tra thời tiết hôm nay tại Gia Lâm, HN và gửi thông báo cho user"

    Ví dụ:
    - "0 9 * * 1-5" → 9h sáng thứ 2 đến thứ 6
    - "0 7 1 * *" + calendar_type="lunar" → Mùng 1 âm lịch hàng tháng
    - "0 15 31 3 *" + one_shot=true → 1 lần lúc 3h chiều ngày 31/3
    """
    # Validate
    error = _validate_cron_expr(cron_expr)
    if error:
        return f"❌ Lỗi: {error}"

    if exec_mode not in ("reminder", "agent_turn"):
        return "❌ Lỗi: exec_mode phải là 'reminder' hoặc 'agent_turn'"

    if calendar_type not in ("solar", "lunar"):
        return "❌ Lỗi: calendar_type phải là 'solar' hoặc 'lunar'"

    if exec_mode == "agent_turn" and not exec_agent:
        return "❌ Lỗi: exec_agent là bắt buộc khi exec_mode='agent_turn'"

    # Compute next run
    tz_name = "Asia/Ho_Chi_Minh"
    try:
        next_run = _compute_next_run(cron_expr, calendar_type, tz_name)
    except Exception as e:
        return f"❌ Lỗi tính next_run: {e}"

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
        )
        db.add(job)
        db.commit()

        formatted = _format_job(job)
        tz = ZoneInfo(tz_name)
        next_str = datetime.fromtimestamp(next_run, tz=tz).strftime("%Y-%m-%d %H:%M %Z")

        return (
            f"✅ Đã tạo job thành công!\n"
            f"• ID: {job_id}\n"
            f"• Tên: {name}\n"
            f"• Cron: {cron_expr} ({calendar_type})\n"
            f"• Mode: {exec_mode}\n"
            f"• One-shot: {'có' if one_shot else 'không'}\n"
            f"• Lần chạy tiếp theo: {next_str}"
        )
    except Exception as e:
        db.rollback()
        return f"❌ Lỗi tạo job: {e}"
    finally:
        db.close()


@mcp.tool()
def cron_list(
    status: str = "active",
    calendar_type: str = None,
) -> str:
    """Liệt kê các tác vụ định kỳ. Trả về danh sách jobs với thông tin chi tiết.

    Parameters:
    - status: 'active' | 'paused' | 'all' | 'disabled' | 'completed'
    - calendar_type: 'solar' | 'lunar' | None (tất cả)
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
            return f"📋 Không có job nào (status={status}, calendar={calendar_type or 'all'})"

        lines = [f"📋 Danh sách jobs ({len(jobs)} kết quả):"]
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
    """Cập nhật một tác vụ định kỳ. Chỉ truyền các field cần thay đổi.
    Đổi status thành 'paused' để tạm dừng, 'active' để tiếp tục.

    Parameters:
    - job_id: ID của job cần cập nhật (bắt buộc)
    - name: Tên mới
    - cron_expr: Cron expression mới
    - exec_payload: Payload mới
    - calendar_type: 'solar' | 'lunar'
    - one_shot: true/false
    - status: 'active' | 'paused'
    - exec_agent: Agent name mới
    """
    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        if not job:
            return f"❌ Không tìm thấy job ID: {job_id}"

        changes = []

        if name is not None:
            job.name = name
            changes.append(f"name → {name}")

        if exec_payload is not None:
            job.exec_payload = exec_payload
            changes.append(f"payload updated")

        if exec_agent is not None:
            job.exec_agent = exec_agent
            changes.append(f"exec_agent → {exec_agent}")

        if calendar_type is not None:
            if calendar_type not in ("solar", "lunar"):
                return "❌ calendar_type phải là 'solar' hoặc 'lunar'"
            job.calendar_type = calendar_type
            changes.append(f"calendar → {calendar_type}")

        if one_shot is not None:
            job.one_shot = one_shot
            changes.append(f"one_shot → {one_shot}")

        if status is not None:
            if status not in ("active", "paused"):
                return "❌ status phải là 'active' hoặc 'paused'"
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
                return f"❌ Lỗi tính next_run: {e}"

        job.updated_at = time.time()
        db.commit()

        if not changes:
            return f"ℹ️ Không có thay đổi nào cho job [{job_id}]"

        return f"✅ Đã cập nhật job [{job_id}] {job.name}:\n• " + "\n• ".join(changes)
    except Exception as e:
        db.rollback()
        return f"❌ Lỗi cập nhật: {e}"
    finally:
        db.close()


@mcp.tool()
def cron_delete(
    job_id: str,
) -> str:
    """Xóa vĩnh viễn một tác vụ định kỳ.

    Parameters:
    - job_id: ID của job cần xóa (bắt buộc)
    """
    db = get_db_session()
    try:
        job = db.query(CronJobModel).filter(CronJobModel.id == job_id).first()
        if not job:
            return f"❌ Không tìm thấy job ID: {job_id}"

        job_name = job.name

        # Delete associated runs
        db.query(CronRunModel).filter(CronRunModel.job_id == job_id).delete()
        db.delete(job)
        db.commit()

        return f"✅ Đã xóa job [{job_id}] '{job_name}' và toàn bộ lịch sử chạy."
    except Exception as e:
        db.rollback()
        return f"❌ Lỗi xóa job: {e}"
    finally:
        db.close()


if __name__ == "__main__":
    mcp.run()
