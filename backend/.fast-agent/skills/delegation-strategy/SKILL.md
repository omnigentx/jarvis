---
name: delegation-strategy
description: >
  Guides task delegation to specialized agents and spawn tools. Determines when to
  self-handle vs delegate, selects appropriate MCP servers and skills for spawned agents.
  Use when receiving research tasks, web access needs, or complex multi-step requests.
---

# Delegation Strategy

## Core Rule

**Do not self-handle tasks requiring web access, research, or specialized tools.**
Delegate to existing agents first, spawn new agents only when no existing agent fits.

## Decision Flow

1. **Existing agent matches?** → Use `agent__<Name>` tool directly
2. **No match, short task (<2 min)?** → `spawn_and_run_isolated`
3. **No match, long task?** → `spawn_and_run_background`

## Existing Agents

| Agent | Domain |
|-------|--------|
| ResearchAgent | Web search, news, information synthesis |
| FinanceAgent | Stock prices, gold, crypto, market analysis |
| PersonalAgent | Email, calendar, reminders (cron scheduler), personal task management |
| CrawlStoriesAgent | Story crawling from web |
| IoTAgent | Smart home device control |
| MusicAgent | Music search and playback |
| AudioReaderAgent | Story audio playback |

> **Note**: PersonalAgent uses the **cron scheduler** for reminders, NOT Google Calendar.
> Google Calendar is only for real events / meetings.

## Spawn Tool Quick Reference

```
spawn_and_run_isolated(
  task="...",           # Specific task description
  instruction="...",    # Agent role and behavior
  servers="serpapi, scrapling-server",  # Comma-separated
  skills="research, scrape-web",       # Comma-separated
  role="researcher",    # Label for tracking
  timeout_seconds=90    # Default 120
)
```

For background: same params, use `spawn_and_run_background`, default timeout 600s.
Check status with `check_spawn_status(run_id)`.

## Choosing Servers and Skills

See [SERVERS.md](SERVERS.md) for MCP server catalog and selection rules.
See [SKILLS.md](SKILLS.md) for skill catalog and pairing guidance.

## Anti-patterns

- ❌ Jarvis self-researching (no direct web server access)
- ❌ Spawning without servers (agent can't do anything)
- ❌ Adding `chrome-devtools` for simple search (use `serpapi`)
- ❌ Empty or vague instruction (always specify role + output format)
- ❌ Missing context (pass relevant conversation context to spawned agent)
