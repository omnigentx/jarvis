"""
Approval MCP Tool Server — agent requests human approval before proceeding.

Blocking mechanism:
  1. POST /api/approvals → create approval request (pauses team)
  2. Subscribe SSE /api/agents/activity-stream → wait for approval_resolved event
  3. Return decision + inline comments to agent

Environment:
  JARVIS_API_KEY  — API auth key (passed via fastagent.config.yaml env)
  JARVIS_API_BASE — Backend URL (default: http://127.0.0.1:8000)
"""

import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ApprovalService")

# Config
API_BASE = os.environ.get("JARVIS_API_BASE", "http://127.0.0.1:8000")
API_KEY = os.environ.get("JARVIS_API_KEY", "")


def _api_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }


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
    """Yêu cầu user phê duyệt trước khi tiếp tục.

    Tool này sẽ BLOCK cho đến khi user approve hoặc reject trên dashboard.
    Tất cả các agent trong team sẽ bị pause trong lúc chờ.

    Parameters:
    - title: Tiêu đề của yêu cầu (ví dụ: "Implementation Plan - Feature X")
    - content: Nội dung cần duyệt (BRD, plan, architecture, etc.)
    - agent_name: Tên agent đang yêu cầu
    - team_name: Tên team (nếu có — cả team sẽ bị pause)
    - approval_type: Loại duyệt (team_plan, architecture, implementation_plan, budget, deploy, custom)
    - urgency: Mức độ ưu tiên (low, normal, high, urgent)
    - content_format: Định dạng nội dung (text, markdown, json)

    - impact_files: Số file bị ảnh hưởng
    - impact_services: Số service bị ảnh hưởng
    - impact_downtime: Thời gian downtime dự kiến
    - impact_risk: Mức độ rủi ro (low, medium, high, critical)

    Returns: Kết quả phê duyệt với decision, comment, và inline comments từ user.
    """
    import httpx

    # 1. Create approval request via API
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


    # Impact analysis
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

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{API_BASE}/api/approvals",
            json=payload,
            headers=_api_headers(),
        )
        if resp.status_code != 201:
            raise RuntimeError(f"Failed to create approval: {resp.status_code} {resp.text}")

        approval = resp.json()
        approval_id = approval["id"]

    # 2. Subscribe to SSE activity stream and wait for approval_resolved
    result = await _wait_for_resolution_via_sse(approval_id)

    # 3. Format response for agent
    decision = result.get("user_decision", "unknown")
    user_comment = result.get("user_comment", "")
    inline_comments = result.get("comments", result.get("inline_comments", []))
    content_lines = content.split("\n")

    lines = [
        f"📋 Approval Result: **{decision.upper()}**",
        f"📝 Title: {title}",
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
                lines.append(f'  • [Lines {sel.get("start_line", "?")}-{sel.get("end_line", "?")}]: "{ctx}"')
                lines.append(f"    → {c['body']}")

    return "\n".join(lines)


async def _wait_for_resolution_via_sse(approval_id: str) -> dict:
    """Subscribe to SSE activity stream and block until approval is resolved.

    Connects to /api/agents/activity-stream and listens for
    event_type == "approval_resolved" with matching approval_id.
    On disconnect, throws error (no fallback, dễ debug).
    """
    import httpx

    sse_url = f"{API_BASE}/api/agents/activity-stream"
    params = {"api_key": API_KEY} if API_KEY else {}

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "GET",
            sse_url,
            params=params,
            headers={"Accept": "text/event-stream"},
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(
                    f"SSE connection failed: {response.status_code}"
                )

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue

                try:
                    event = json.loads(line[6:])
                except (json.JSONDecodeError, ValueError):
                    continue

                # Skip pings and other events
                event_type = event.get("event_type") or event.get("type")
                if event_type != "approval_resolved":
                    continue

                # Check if this event matches our approval
                data = event.get("data", {})
                if data.get("approval_id") != approval_id:
                    continue

                # Match! Fetch full approval detail with comments
                # Use a separate client to avoid conflict with SSE stream
                async with httpx.AsyncClient(timeout=30) as detail_client:
                    detail_resp = await detail_client.get(
                        f"{API_BASE}/api/approvals/{approval_id}",
                        headers=_api_headers(),
                    )
                if detail_resp.status_code == 200:
                    return detail_resp.json()
                else:
                    # Return what we have from the event
                    return {
                        "user_decision": data.get("decision", "unknown"),
                        "user_comment": data.get("comment", ""),
                        "comments": [],
                    }

    # Stream ended without receiving resolution — should not happen
    raise RuntimeError(
        f"SSE stream ended without receiving approval resolution for {approval_id}"
    )


if __name__ == "__main__":
    mcp.run()
