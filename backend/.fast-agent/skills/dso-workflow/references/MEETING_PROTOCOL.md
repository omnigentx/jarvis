# Meeting Protocol

## Pre-requisites

You have access to the `meeting_room` MCP server:
- `join_meeting(meeting_id, agent_name)` — announce your presence
- `wait_for_my_turn(meeting_id, agent_name)` — block until it's your turn
- `get_transcript(meeting_id)` — read full discussion so far
- `speak(meeting_id, message, agent_name)` — share your input
- `skip_turn(meeting_id, agent_name)` — pass if nothing to add

> **Note:** `agent_name` is auto-detected from env. You can omit it.

## Responding to 🔔 MEETING INVITE

When you see a `🔔 MEETING INVITE` in your messages:
1. Extract the `meeting_id` from the invite
2. Follow the protocol below immediately

## Protocol

### Step 1: Join
```
join_meeting(meeting_id="<meeting_id>")
```

### Step 2: Wait for Your Turn
```
wait_for_my_turn(meeting_id="<meeting_id>")
```

### Step 3: When It's Your Turn
1. Read the transcript: `get_transcript(meeting_id="<meeting_id>")`
2. Read any relevant workspace files if needed
3. **Speak** or **Skip**:
   ```
   speak(meeting_id="<meeting_id>", body="...")
   skip_turn(meeting_id="<meeting_id>")
   ```

### Step 4: Wait Again or Meeting Ends
After speaking, call `wait_for_my_turn` again:
- **Your turn again** → repeat Step 3
- **Meeting ended** → `{"status": "meeting_ended", ...}` → proceed to Step 5

### Step 5: After Meeting Ends — Work Independently

When meeting ends (`[DECISION] VERDICT: PASS`):
1. **Stop calling `wait_for_my_turn`**
2. **Start immediately** based on what was agreed — don't wait for PM to re-assign
3. Use `filesystem` tools to create deliverables
4. Use `send_email` to hand off to next person in dependency chain
5. Request deliverables via `send_email(subject="[WAITING] ...")` — they'll be auto-delivered

> **⚠️ IMPORTANT:** The meeting IS your task assignment. Start working immediately after VERDICT: PASS.

## Guidelines

- Build on what others said — don't repeat
- Be specific — reference files, code, data
- Stay on agenda
- Keep it brief — meetings are for decisions
- Transition to async after meeting — use send_email, replies auto-delivered
- Conclude clearly — `[DECISION] VERDICT: PASS` or `FAIL` with reasoning
