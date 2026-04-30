---
name: personal-assistant
description: >
  Manage calendar (Google Calendar) and email (Gmail). Use when user requests: view/create/delete
  calendar events, send/read email, reminders. MUST call get_current_time before any time operation.
---

# CALENDAR & EMAIL MANAGEMENT

<prerequisite>
ALWAYS call `get_current_time` BEFORE any time-related operation.
NEVER guess the year, month, or day. Use real-time data only.
</prerequisite>

## Decision Tree

```
What does the user want?
├── View calendar → calendar_list_events(timeMin, timeMax)
├── Create event/meeting → calendar_create_event(summary, start, end)
│   └── Need Meet link? → Auto-created with event
├── Delete event → (1) list_events → (2) get eid → (3) delete_event(eid)
├── Send email → gmail_send(to, subject, body)
├── Read email → gmail_list() → gmail_read(id)
└── Reminder/nhắc nhở → Use cron_create (cron-management skill)
    └── "nhắc tôi", "đừng quên", "nhắc nhở" → cron_create with exec_mode="reminder"
    └── KHÔNG dùng Google Calendar cho reminders. Dùng cron scheduler.
```

<rule>
1. Call each tool ONLY ONCE per request.
2. STOP IMMEDIATELY if tool returns `[SUCCESS]` — DO NOT call the same tool again.
3. If tool returns `WARNING` → read carefully and ask user for clarification.
</rule>

## Delete Event — Workflow

1. `calendar_list_events` → find the event
2. Get `eid` from URL (part after `eid=`)
3. `calendar_delete_event(eid)`
4. Call list_events ONLY ONCE → DELETE immediately

<violation>
- Calling `calendar_create_event` twice → creates duplicate → VIOLATION
- Not calling `get_current_time` → guessing wrong year → VIOLATION
- Tool returned SUCCESS and you call it again → VIOLATION
</violation>

## ✅ Correct Examples
- "Created meeting 'Sprint Review' at 2pm Monday March 17."
- "Deleted event 'Team Meeting' tomorrow."
