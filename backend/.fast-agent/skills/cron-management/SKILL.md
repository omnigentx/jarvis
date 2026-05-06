---
name: cron-management
description: >
  Manage scheduled jobs (cron). Use when the user wants to create, edit,
  or delete a reminder, recurring schedule, or automated agent task
  (agent_turn). MUST call get_current_time before creating a one-shot
  job so the absolute date/time is accurate.
---

# Cron Management Skill

<violation>
- DO NOT read files, browse code, or run shell commands to "learn" the cron system.
- DO NOT use read_text_file, execute, grep, or any tool other than the cron tools.
- ONLY call directly: `cron_create`, `cron_list`, `cron_update`, `cron_delete`, `get_current_time`.
- Violating this wastes tokens and slows the request.
</violation>

You have 4 ready-to-call tools: `cron_create`, `cron_list`, `cron_update`, `cron_delete`. No discovery needed.

## Cron expression rules (REQUIRED)

A cron expression has 5 fields: `minute hour day_of_month month day_of_week`.

### Common examples
- `0 9 * * *` → every day at 9:00
- `0 9 * * 1-5` → 9:00 Monday through Friday
- `*/30 * * * *` → every 30 minutes
- `0 */4 * * *` → every 4 hours
- `0 7 27 4 *` → April 27 at 7:00 every year
- `0 15 31 3 *` + one_shot=true → once on March 31 at 15:00

## Calendar type (REQUIRED)

- **Solar (Gregorian)** — DEFAULT for normal date/month references.
- **Lunar** — use ONLY when the user explicitly references lunar concepts (1st-of-month, full-moon, Tết, lunar death anniversary, etc.).
  - `0 7 15 * *` + calendar_type="lunar" → 15th of every lunar month
  - `0 6 1 * *` + calendar_type="lunar" → 1st of every lunar month
  - `0 7 10 3 *` + calendar_type="lunar" → 10/3 lunar

## Exec mode (REQUIRED)

| User intent | Exec mode | Meaning |
|---|---|---|
| Plain reminder ("remind me") | `reminder` | Send a notification text to the user |
| Action ("summarise / analyse / check / crawl / find") | `agent_turn` | An agent executes a task automatically |

- `exec_mode = "reminder"` → `exec_payload` is the reminder text. **No** `exec_agent` needed.
- `exec_mode = "agent_turn"` → `exec_payload` is the **direct execution prompt**, `exec_agent` is the agent name (REQUIRED). Valid agents:
  - `jarvis` — synthesis, analysis, complex queries
  - `ResearchAgent` — web search, news
  - `FinanceAgent` — stock / gold / crypto prices

### ⚠️ Writing exec_payload for agent_turn (IMPORTANT)

`exec_payload` is the prompt sent to the agent when the cron triggers — it must be a **direct action prompt**.

**DO NOT** copy the user's scheduling phrasing verbatim (e.g. "Every day at 7am, please...").
**DO** rewrite it as an action prompt:

| ❌ WRONG (scheduling text copied in) | ✅ RIGHT (action prompt) |
|---|---|
| "Every day at 7am, check the weather in HN" | "Check today's weather in Gia Lam, HN and send a notification" |
| "Daily 8am summarise AI news" | "Summarise the day's notable AI news as bullet points" |
| "Remind me every Saturday morning to log TAS" | (use exec_mode=reminder, not agent_turn) |

## Pause / resume

- Pause: `cron_update(job_id="...", status="paused")`
- Resume: `cron_update(job_id="...", status="active")`

## Always confirm with the user

After creating a job, summarise back:
- job name
- schedule (explained in the user's language)
- mode (reminder vs AI-executed)
- next run time
