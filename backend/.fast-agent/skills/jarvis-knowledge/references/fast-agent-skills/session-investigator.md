---
name: session-investigator
description: Investigate fast-agent session and history files to diagnose issues. Use when a session ended unexpectedly, when debugging tool loops, when correlating sub-agent traces with main sessions, or when analyzing conversation flow and timing.
---
# Session Investigator
Diagnose fast-agent session issues by examining session and history files.
## Session Directory Structure
Sessions are stored in `.fast-agent/sessions/<id>/`:
```
2601181023-Kob2h3/
├── session.json              # Session metadata
├── history_<agent>.json      # Current agent history
└── history_<agent>_previous.json  # Previous save (rotation backup)
```
Session IDs encode creation time: `YYMMDDHHMM-<random>`.
## Key Files
### session.json
```json
{
  "name": "2601181023-Kob2h3",
  "created_at": "2026-01-18T10:23:24.116526",
  "last_activity": "2026-01-18T10:39:42.873467",
  "history_files": ["history_dev_previous.json", "history_dev.json"],
  "metadata": {
    "agent_name": "dev",
    "first_user_preview": "is it possible to override..."
  }
}
```
### history_<agent>.json
Messages array with `role`, `content`, `tool_calls`, `tool_results`, `channels` (timing, reasoning).
## Investigation Commands
### Basic inspection
```bash
jq '.messages | length' history_dev.json
jq '.messages[-5:] | .[] | {role, stop_reason, has_tool_calls: (.tool_calls != null)}' history_dev.json
```
### Tool call correlation
```bash
jq '.messages[-10:] | to_entries | .[] | {
 index: .key,
 role: .value.role,
 tool_calls: (if .value.tool_calls then (.value.tool_calls | keys) else [] end),
 tool_results: (if .value.tool_results then (.value.tool_results | keys) else [] end)
}' history_dev.json
```
### LLM Call Stats
```bash
jq '[.messages[] | select(.role == "assistant") |
 select(.channels."fast-agent-timing") |
 .channels."fast-agent-timing"[0].text | fromjson | .duration_ms] |
 {count: length, total_ms: add, avg_ms: (add/length), max_ms: max}' history_dev.json
```
## Common Failure Patterns
### Unanswered Tool Call
**Symptom**: API error "No tool output found for function call"
**Pattern**: History ends with assistant message having `tool_calls` and `stop_reason: "toolUse"`, but no matching `tool_results`.
**Fix**: Truncate history to last valid tool result.
### Duplicate User Messages
**Pattern**: Two consecutive `user` messages before assistant response.
**Cause**: Often from `before_llm_call` hooks appending instructions.
## Source
- Repository: https://github.com/fast-agent-ai/skills
- Path: skills/session-investigator/
