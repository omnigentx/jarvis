---
name: team-communication
description: >
  Communication rules within a team (email, meetings). Use when the agent
  needs to send an email, join a meeting, or report a result. Tools:
  send_email, create_meeting.
---

# Team Communication Skill

<violation>
- DO NOT read files, browse code, or run shell commands to "learn" the communication system.
- DO NOT use read_text_file, execute, grep, or any tool other than the communication tools.
- ONLY call directly: `send_email`, `create_meeting`.
- Violating this wastes tokens and slows the request.
</violation>

## ⚡ Auto status notifications

Team-mate results are **auto-delivered** to your inbox once ALL members finish:
- A consolidated report (table) with name, status, summary.
- **Do NOT poll.** Stay focused on your own task; the report arrives when everyone is done.
- If you need richer detail, use `send_email` or `create_meeting` to engage a specific member.

## 📧 Email — async, fire-and-forget

- `send_email(to="Name", body="...", subject="...")` — send to a specific person
- `send_email(to="all", body="...")` — broadcast to the whole team
- Emails from team-mates are **auto-delivered** into your context — no polling required

## Waiting for dependencies

If you need a team-mate's output:
1. Send the request: `send_email(to="Agent Name", body="Please send me <deliverable> when ready", subject="[WAITING] ...")`
2. Move on to other work or finish your current task.
3. When the team-mate replies, you are auto-woken with the content.
4. If you need outputs from several members, call `create_meeting` for a quick sync instead.

## 🎙️ Meeting — real-time decisions

When you receive 🔔 MEETING INVITE → follow the `meeting-participant` skill to join and speak.

## Email discipline

- **Focus on the task first.** Only email when you have a deliverable, are blocked, or spot a critical issue. Do NOT email merely to update status or acknowledge.
- **Concise but complete.** Include enough context. Use subject prefixes: `[DONE]`, `[BLOCKED]`, `[BUG]`, `[REVIEW]`, `[DELIVERABLE]`, `[WAITING]`.
- **Use CC sparingly.** Only CC people who genuinely need to know.
- **Do NOT reply just to acknowledge.** Use `no_reply=True` for FYI messages.
- **Avoid email ping-pong.** If you need back-and-forth, schedule a meeting instead of emailing.

## Completion rules (REQUIRED)

BEFORE going idle or completing work, you MUST:
1. Send a `[DONE]` report to the PM: `send_email(to="Linh - PM", subject="[DONE] <deliverable summary>", body="<list of deliverables, files, outcomes, open items>")`
2. NEVER go idle without sending the report — this is **required**.

## Approval escalation — when you need permission for a sensitive action

Some shell / git / `gh` commands are flagged 🟡 ESCALATE in your role skill (`dev-workflow`, `qe-workflow`, `jarvis-infra`). When you hit one:

1. Do NOT run the command. Send an approval-request email to the PM:
   ```python
   send_email(
       to="<PM name>",
       subject="[APPROVAL-REQUEST] <one-line summary of action>",
       body="""
   Need approval to run: `<exact command>`
   Why: <task this unblocks>
   Risk: <what could go wrong>
   Alternatives: <list, or 'none — only path forward'>
   """,
   )
   ```
2. Stop and wait for the PM to reply. PM will relay the request to the user via the approval-server MCP, then email you `[APPROVED]` or `[DENIED]` with the verdict.
3. **If APPROVED** — run the exact command from the request (no edits). Report result.
4. **If DENIED** — do not retry. Re-plan or report the block to PM.

### PM responsibilities (PM role only)

When PM receives `[APPROVAL-REQUEST]`:
- Verify the requesting agent is permitted to ask (Dev/QE/DSO can; Designer/BA/SA cannot run shell writes — should be redirected to a different approach).
- Call `request_approval(action=<command>, reason=<why+risk>)` from the `approval-server` MCP. User sees a dashboard prompt and approves/rejects.
- Email the requester back with `[APPROVED]` or `[DENIED] <reason>` so they proceed or abort.
