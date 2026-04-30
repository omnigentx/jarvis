"""Rewrite googleapiclient errors into actionable, user-friendly strings.

The raw ``HttpError`` traceback dumped into the MCP response is a wall of
unhelpful JSON. For the common case where the user hasn't enabled the
required API on their Cloud project yet (HTTP 403 ``accessNotConfigured``),
we swap in a one-line explanation + a direct "click here to enable" URL so
the fix is a single click away.
"""
from __future__ import annotations

import json
import re

from googleapiclient.errors import HttpError

# Parse the two fragments we need out of Google's error message:
#   "Gmail API has not been used in project 315696637373 before or it is
#    disabled. Enable it by visiting https://console.developers.google.com/
#    apis/api/gmail.googleapis.com/overview?project=315696637373 then retry."
_PROJECT_RE = re.compile(r"project (\d+)")
_API_RE = re.compile(r"([a-z0-9-]+\.googleapis\.com)")


def format_api_error(exc: Exception) -> str:
    """Return a human-friendly string for ``exc``; fall back to ``str(exc)``."""
    if not isinstance(exc, HttpError):
        return str(exc)
    content = exc.content
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8", errors="replace")
        except Exception:
            return str(exc)
    try:
        body = json.loads(content)
    except Exception:
        return str(exc)

    errors = (body.get("error") or {}).get("errors") or []
    for err in errors:
        if err.get("reason") != "accessNotConfigured":
            continue
        msg = err.get("message", "")
        api_match = _API_RE.search(msg)
        project_match = _PROJECT_RE.search(msg)
        api_id = api_match.group(1) if api_match else "gmail.googleapis.com"
        project = project_match.group(1) if project_match else None
        link = f"https://console.developers.google.com/apis/api/{api_id}/overview"
        if project:
            link = f"{link}?project={project}"
        return (
            f"{api_id} is not enabled for this Google Cloud project. "
            f"Open this link, click 'Enable', wait ~1 minute, then retry:\n{link}"
        )
    return str(exc)
