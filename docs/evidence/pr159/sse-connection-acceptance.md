# Chat and monitor SSE connection acceptance (2026-09-27)

## Reproduction before the fix

Local backend: `127.0.0.1:8012`; Vite frontend: `127.0.0.1:3005`. The backend used a clone of the local test database and the Atlassian Cloud test tenant. No production deployment was involved.

1. In Chrome, open Chat alone. Input: `Kiểm tra kết nối localhost. Trả lời đúng một từ: OK.` UI output: `OK`; the backend log contained the corresponding Jarvis turn.
2. Open Team Monitor in a second tab of the same Chrome browser. Input in the existing Chat conversation: `Lần kiểm tra thứ hai. Trả lời đúng một từ: OK2.` UI showed a pending reply; the backend log still ended at the first turn. Closing the Monitor tab caused the backend to receive the second input immediately, followed by UI output `OK2`.
3. In the Codex in-app browser, two localhost tabs likewise left two Chat messages pending. Screenshot: [chat-stalled-two-requests.png](chat-stalled-two-requests.png). This corroborated the symptom but was not the controlled before/after test.

The old `AppLayout.vue` opened three global EventSource connections per tab (activity, scheduler, MCP). Two tabs occupied six long-lived same-origin connections. The Chrome open/close test above supports browser connection saturation as the cause of the delayed POST. The repeatedly logged `statusFooter` Vue warning is a separate observation; it was not needed to explain this POST delay.

## Change and retest

`AppLayout.vue` now uses the existing activity SSE for MCP warnings and notification badges. `SchedulerStreamManager.broadcast()` forwards only `new_notification` events to the activity stream, while retaining the scheduler stream for its dedicated dashboard. The activity stream reconnect callback refreshes the authoritative unread count.

1. Reload Chat with the changed frontend and open Team Monitor in a second Chrome tab. Input: `Lần kiểm tra sau sửa SSE. Trả lời đúng một từ: OK3.` The backend received the input without closing Monitor; UI output: `OK3`. Screenshot: [chat-recovers-with-monitor-open.png](chat-recovers-with-monitor-open.png).
2. Restart the local backend with `JARVIS_API_KEY=<local test value> UV_FROZEN=1 uv run uvicorn server:app --host 127.0.0.1 --port 8012`, then sign in again. With Monitor still open, input: `Sau restart backend, trả lời đúng một từ: OK4.` UI output: `OK4`. Screenshot: [chat-after-restart-monitor-open.png](chat-after-restart-monitor-open.png).
3. Create a one-shot reminder in the isolated test database (`job_id=8fe4986a`). On execution, the scheduler recorded one completed run and notification `id=88`. The Chat tab badge advanced from 3 to 4 through the shared activity stream. Screenshot: [notification-badge-via-activity-stream.png](notification-badge-via-activity-stream.png). Both probe records were deleted afterwards; SQL checks returned zero rows for both IDs.

## Automated checks

- Backend: `test_cron_scheduler_agent_turn.py`, `test_scheduler_activity_bridge.py`, `test_mcp_runtime.py` — **13 passed**, one pre-existing SQLAlchemy deprecation warning.
- Frontend: `npm run test:unit` — **201 passed**.
- Frontend: `npm run build` — **passed**; existing chunk-size and ineffective dynamic-import warnings remain.
- `git diff --check` — **passed**.

This establishes the two-tab Chat happy case and notification delivery. It does not establish five-or-more-tab behavior, cross-tab sharing, or SCRUM-30 duplicate-PM identity/restart acceptance. PR #159 remains Draft until its broader acceptance gates are resolved.
