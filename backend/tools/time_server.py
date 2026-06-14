import os

from mcp.server.fastmcp import FastMCP
from datetime import datetime
from zoneinfo import ZoneInfo
from lunar_python import Lunar
from lunar_python import Solar


def _get_tz() -> ZoneInfo:
    tz_name = os.environ.get("JARVIS_TIMEZONE", "Asia/Ho_Chi_Minh")
    return ZoneInfo(tz_name)  # ZoneInfoNotFoundError propagates — bad timezone is an explicit config error

# Explicit English weekday names so the output never depends on the host
# locale (strftime('%A') would return Vietnamese under LANG=vi_VN). The tool is
# LLM-facing — English in, the agent renders the user's language out (§7).
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _weekday(d: datetime) -> str:
    return _WEEKDAYS[d.weekday()]

# Initialize FastMCP server
mcp = FastMCP("TimeService")

@mcp.tool()
def get_current_time() -> str:
    """Get the current time and weekday (Vietnam timezone by default)."""
    now = datetime.now(_get_tz())
    return f"It is {now.strftime('%H:%M')} on {_weekday(now)}, {now.strftime('%d/%m/%Y')}."

@mcp.tool()
def solar_to_lunar(day: int, month: int, year: int) -> str:
    """Convert a Solar (Gregorian) date to its Lunar equivalent."""
    solar = Solar.fromYmd(year, month, day)
    lunar = solar.getLunar()
    return f"Solar {day}/{month}/{year} is Lunar {lunar.getDay()}/{lunar.getMonth()}/{lunar.getYear()}"

@mcp.tool()
def lunar_to_solar(day: int, month: int, year: int, leap_month: bool = False) -> str:
    """Convert a Lunar date to its Solar (Gregorian) equivalent."""
    # Note: lunar_python uses year, month, day order
    lunar = Lunar.fromYmd(year, month, day)
    solar = lunar.getSolar()
    return f"Lunar {day}/{month}/{year} is Solar {solar.getDay()}/{solar.getMonth()}/{solar.getYear()}"

@mcp.tool()
def get_lunar_date() -> str:
    """Return today's Lunar date as 'DD/MM/YYYY'."""
    d = datetime.now(_get_tz())
    lunar = Lunar.fromDate(d)
    return f"{lunar.getDay()}/{lunar.getMonth()}/{lunar.getYear()}"

@mcp.tool()
def get_solar_date() -> str:
    """Return today's Solar (Gregorian) date as 'DD/MM/YYYY'."""
    d = datetime.now(_get_tz())
    return d.strftime("%d/%m/%Y")

@mcp.tool()
def get_today_info() -> str:
    """Return the weekday plus Solar and Lunar representations of today's date."""
    now = datetime.now(_get_tz())
    solar = get_solar_date()
    lunar = get_lunar_date()
    return f"Today is {_weekday(now)}, {solar} (Solar) / {lunar} (Lunar)."

@mcp.tool()
async def wait_for_seconds(seconds: int) -> str:
    """Sleep for the given number of seconds. Useful when waiting for an
    email to arrive or a device to respond.

    Args:
        seconds: how long to wait, in seconds.
    """
    import asyncio
    await asyncio.sleep(seconds)
    return f"Waited {seconds} seconds."

if __name__ == "__main__":
    mcp.run()
