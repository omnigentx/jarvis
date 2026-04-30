import os

from mcp.server.fastmcp import FastMCP
from datetime import datetime
from zoneinfo import ZoneInfo
from lunar_python import Lunar
from lunar_python import Solar


def _get_tz() -> ZoneInfo:
    tz_name = os.environ.get("JARVIS_TIMEZONE", "Asia/Ho_Chi_Minh")
    return ZoneInfo(tz_name)  # ZoneInfoNotFoundError propagates — bad timezone is an explicit config error

# Initialize FastMCP server
mcp = FastMCP("TimeService")

@mcp.tool()
def get_current_time() -> str:
    """Get the current time in Vietnam (Gia Lam, Hanoi context)."""
    now = datetime.now(_get_tz())
    return f"Bây giờ là {now.strftime('%H:%M')} ngày {now.strftime('%d/%m/%Y')}."

@mcp.tool()
def solar_to_lunar(day: int, month: int, year: int) -> str:
    """Convert Solar date to Lunar date."""
    solar = Solar.fromYmd(year, month, day)
    lunar = solar.getLunar()
    return f"Dương lịch {day}/{month}/{year} là Âm lịch {lunar.getDay()}/{lunar.getMonth()}/{lunar.getYear()}"

@mcp.tool()
def lunar_to_solar(day: int, month: int, year: int, leap_month: bool = False) -> str:
    """Convert Lunar date to Solar date."""
    # Note: lunar_python uses year, month, day order
    lunar = Lunar.fromYmd(year, month, day)
    solar = lunar.getSolar()
    return f"Âm lịch {day}/{month}/{year} là Dương lịch {solar.getDay()}/{solar.getMonth()}/{solar.getYear()}"

@mcp.tool()
def get_lunar_date() -> str:
    """
    Lấy ngày tháng năm Âm lịch hiện tại.
    Returns:
        str: Ngày âm lịch định dạng 'DD/MM/YYYY'
    """
    d = datetime.now(_get_tz())
    lunar = Lunar.fromDate(d)
    return f"{lunar.getDay()}/{lunar.getMonth()}/{lunar.getYear()}"

@mcp.tool()
def get_solar_date() -> str:
    """
    Lấy ngày tháng năm Dương lịch hiện tại.
    Returns:
        str: Ngày dương lịch định dạng 'DD/MM/YYYY'
    """
    d = datetime.now(_get_tz())
    return d.strftime("%d/%m/%Y")

@mcp.tool()
def get_today_info() -> str:
    """
    Lấy thông tin chi tiết về ngày hôm nay (Dương lịch và Âm lịch).
    Returns:
        str: Thông tin ngày
    """
    solar = get_solar_date()
    lunar = get_lunar_date()
    return f"Hôm nay là ngày {solar} (Dương lịch), tức ngày {lunar} (Âm lịch)."

@mcp.tool()
async def wait_for_seconds(seconds: int) -> str:
    """
    Dừng chờ trong một khoảng thời gian (giây).
    Hữu ích khi cần đợi email đến hoặc đợi thiết bị phản hồi.
    Args:
        seconds: Số giây cần chờ.
    """
    import asyncio
    await asyncio.sleep(seconds)
    return f"Đã chờ {seconds} giây."

if __name__ == "__main__":
    mcp.run()
