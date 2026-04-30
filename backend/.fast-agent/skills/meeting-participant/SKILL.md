---
name: meeting-participant
description: Protocol for joining and participating in multi-agent meetings
---

# Meeting Participant Skill

You are joining a team meeting. Follow this protocol exactly.

## 📧 Email vs 🎙️ Meeting — When to Use Which

| Channel | Tool | Speed | Use For |
|---------|------|-------|---------|
| 📧 Email | `send_email` | Async (reply later) | Task assignments, status updates, deliverables |
| 🎙️ Meeting | `create_meeting` → join → speak | Real-time (immediate) | Kickoff, design review, code review, blockers |

**Meetings give FASTER feedback** — all participants discuss and decide in the same session.

## Pre-requisites

You have access to the `meeting_room` MCP server which provides these tools:
- `join_meeting(meeting_id, agent_name)` — announce your presence
- `wait_for_my_turn(meeting_id, agent_name)` — block until it's your turn
- `get_transcript(meeting_id)` — read full discussion so far
- `speak(meeting_id, message, agent_name)` — share your input
- `skip_turn(meeting_id, agent_name)` — pass if nothing to add

> **Note:** `agent_name` is auto-detected from env. You can omit it.

## Responding to 🔔 MEETING INVITE

When you see a `🔔 MEETING INVITE` in your messages, you MUST:

1. Extract the `meeting_id` from the invite
2. Follow the protocol below immediately

## Protocol

### Step 1: Join the Meeting
```
join_meeting(meeting_id="<meeting_id>")
```

### Step 2: Wait for Your Turn
```
wait_for_my_turn(meeting_id="<meeting_id>")
```
This will block until the meeting facilitator gives you the floor.

### Step 3: When It's Your Turn

1. Read the transcript: `get_transcript(meeting_id="<meeting_id>")`
2. Read any relevant workspace files if needed
3. **Speak** with your input:
   ```
   speak(meeting_id="<meeting_id>", body="...")
   ```
   OR **Skip** if you have nothing new to add:
   ```
   skip_turn(meeting_id="<meeting_id>")
   ```

### Step 4: Wait Again or Meeting Ends
After speaking, call `wait_for_my_turn` again. Two things can happen:
- **Your turn again** → repeat Step 3
- **Meeting ended** → you receive `{"status": "meeting_ended", ...}` → **proceed to Step 5**

### Step 5: After Meeting Ends — Work Independently

When `wait_for_my_turn` returns `{"status": "meeting_ended", ...}`:

1. **Stop calling `wait_for_my_turn`** — the meeting is over
2. **You already know your task from the meeting discussion** — start immediately based on what was agreed. Do NOT wait for the facilitator to re-assign via send_email.
3. **Use filesystem tools** to create your deliverables in the agreed directories
4. **Use `send_email`** to hand off deliverables to the next person in the dependency chain
5. **If you need output from others**, request it: `send_email(to="...", body="Send me [deliverable] when ready", subject="[WAITING] ...")` — it will be auto-delivered

> **⚠️ IMPORTANT:** The meeting IS your task assignment. If you participated and acknowledged, you know what to do — start immediately once the meeting ends.

## Guidelines

- **Build on what others said** — don't repeat points already made
- **Be specific** — reference files, code, or data from the workspace
- **Stay on agenda** — focus on the meeting's stated agenda
- **Keep it brief** — meetings are for decisions, not for doing work
- **Transition to async after meeting** — use `send_email` for follow-ups, emails auto-delivered
- **Don't end the meeting yourself** — only the facilitator concludes. The meeting ends when you receive `{"status": "meeting_ended"}` from `wait_for_my_turn`, not because of anything you say.
- **Raise blockers in natural language** — if you think the goal is reached, you're stuck, or the team is going in circles, describe the situation in plain words (e.g. "I need SA input before I can proceed" or "I believe we've covered the agenda"). The facilitator decides how to resolve it.
