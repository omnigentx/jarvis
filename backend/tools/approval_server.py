"""Approval MCP Tool Server — agent requests human approval before proceeding.

Talks to the live backend over the Runtime RPC Unix socket
(``JARVIS_RUNTIME_RPC_SOCKET``); see ``services/runtime_rpc.py`` and
``services/approval_rpc_handlers.py``. No HTTP, no API key — trust is
file-system permissions on the socket path, the same model used by
``skill_server`` and ``mcp_admin``.

Blocking mechanism:
  1. ``approval.create`` — create approval row, pause team, return id
  2. ``approval.wait``  — long-poll until user resolves on dashboard
                          (may block for hours; backend handler is
                          registered with timeout=None and we pass
                          timeout=None to the client too)
  3. Format the resolution payload for the agent

Backend restart resilience: ``approval.wait`` is retried with
exponential-ish backoff on connection drop. The server-side handler
checks DB state on (re)subscribe, so a retry after a restart sees the
already-resolved approval and returns immediately.

Environment:
  JARVIS_RUNTIME_RPC_SOCKET — UDS path to the backend's RuntimeRpcServer
                              (set automatically by the backend at boot,
                              propagated via fastagent.config.yaml).
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Allow imports from backend/.
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.runtime_rpc_client import RuntimeRpcError, call as rpc_call  # noqa: E402

logger = logging.getLogger("approval_server")
mcp = FastMCP("ApprovalService")


# Backend restart / transient socket failures: retry the wait. Each
# reconnect re-issues approval.wait; the handler re-reads DB state, so
# any resolution that landed during the gap is delivered immediately.
#
# Infinite retry IS by design — human approvals legitimately span
# hours-to-days, and a transient socket drop (backend restart, brief
# network blip) is recoverable. A hard cap would convert a 30-second
# restart into an uncaught failure that wedges the agent. We rely on
# exponential backoff to keep log volume bounded if the backend stays
# down for hours, and on the user/operator to notice the backend is
# unreachable through other channels (dashboard, healthcheck).
_WAIT_INITIAL_DELAY_SECONDS = 2.0
_WAIT_MAX_DELAY_SECONDS = 30.0


@mcp.tool()
async def request_approval(
    title: str,
    content: str,
    agent_name: str = "agent",
    team_name: str = None,
    approval_type: str = "custom",
    urgency: str = "normal",
    content_format: str = "text",

    impact_files: int = None,
    impact_services: int = None,
    impact_downtime: str = None,
    impact_risk: str = None,
) -> str:
    """Request human approval before continuing.

    This tool BLOCKS until the user approves or rejects via the dashboard.
    Every agent in the same team is paused while waiting.

    Parameters:
    - title: short title for the request (e.g. "Implementation Plan - Feature X")
    - content: payload to be reviewed (BRD, plan, architecture, etc.)
    - agent_name: name of the requesting agent
    - team_name: team name, if any — the whole team is paused
    - approval_type: one of team_plan / architecture / implementation_plan /
      budget / deploy / custom
    - urgency: low / normal / high / urgent
    - content_format: text / markdown / json

    - impact_files: number of files impacted
    - impact_services: number of services impacted
    - impact_downtime: expected downtime
    - impact_risk: low / medium / high / critical

    Returns: approval result with decision, comment, and inline comments
    from the user.
    """
    payload = {
        "title": title,
        "content": content,
        "agent_name": agent_name,
        "approval_type": approval_type,
        "urgency": urgency,
        "content_format": content_format,
    }
    if team_name:
        payload["team_name"] = team_name

    impact = {}
    if impact_files is not None:
        impact["files"] = impact_files
    if impact_services is not None:
        impact["services"] = impact_services
    if impact_downtime is not None:
        impact["downtime"] = impact_downtime
    if impact_risk is not None:
        impact["risk"] = impact_risk
    if impact:
        payload["impact"] = impact

    # Sync RPC client → run in a worker thread so the MCP loop keeps
    # serving other tool calls while we block on the human.
    create_resp = await asyncio.to_thread(rpc_call, "approval.create", payload)
    if isinstance(create_resp, dict) and "error" in create_resp:
        raise RuntimeError(
            f"Failed to create approval: {create_resp.get('status')} {create_resp.get('error')}"
        )
    approval_id = create_resp["id"]

    # Block until resolved. Auto-retry across backend restarts: each
    # reconnect's handler re-reads DB state and returns immediately if
    # the approval was resolved during the disconnect window.
    delay = _WAIT_INITIAL_DELAY_SECONDS
    while True:
        try:
            result = await asyncio.to_thread(
                rpc_call, "approval.wait", {"approval_id": approval_id}, timeout=None,
            )
            break
        except RuntimeRpcError as exc:
            logger.warning(
                "[approval] RPC dropped while waiting on %s — retrying in %.1fs: %s",
                approval_id, delay, exc,
            )
            await asyncio.sleep(delay)
            # Backoff so an extended outage (backend down for hours)
            # doesn't fill the log with one warning every 2 s.
            delay = min(delay * 2, _WAIT_MAX_DELAY_SECONDS)

    if isinstance(result, dict) and "error" in result and "status" in result:
        # 404 / handler error — surface to the agent rather than guessing.
        raise RuntimeError(
            f"approval.wait failed: {result.get('status')} {result.get('error')}"
        )

    return _format_result(result, content)


def _format_result(result: dict, original_content: str) -> str:
    decision = result.get("user_decision", "unknown")
    user_comment = result.get("user_comment", "")
    inline_comments = result.get("inline_comments", []) or result.get("comments", [])
    content_lines = original_content.split("\n")

    # Title is always present in the dict shape returned by approval.get
    # / approval.wait today. Fall back to the approval id if a future
    # response shape change drops it, so the agent at least sees an
    # identifier instead of a blank "Title:" line.
    title_or_id = result.get("title") or result.get("id") or "(unknown)"
    lines = [
        f"📋 Approval Result: **{decision.upper()}**",
        f"📝 Title: {title_or_id}",
    ]
    if user_comment:
        lines.append(f"💬 User Comment: {user_comment}")
    if inline_comments:
        lines.append(f"\n📌 Inline Comments ({len(inline_comments)}):")
        for c in inline_comments:
            if c.get("line_number") is not None:
                ln = c["line_number"]
                ctx = content_lines[ln - 1].strip() if ln <= len(content_lines) else "(unknown)"
                lines.append(f'  • [Line {ln}]: "{ctx}"')
                lines.append(f"    → {c['body']}")
            elif c.get("selection"):
                sel = c["selection"]
                ctx = sel.get("selected_text", "")
                lines.append(
                    f'  • [Lines {sel.get("start_line", "?")}-{sel.get("end_line", "?")}]: "{ctx}"'
                )
                lines.append(f"    → {c['body']}")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
