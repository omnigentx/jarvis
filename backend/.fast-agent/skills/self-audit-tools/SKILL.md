---
name: self-audit-tools
description: >
  Protocol for an agent to audit its OWN visible toolset. Read when a task
  asks you to "audit your tools" / "list and verify your capabilities" /
  "self-audit". Defines what to call vs. what to only describe, the
  markdown exchange format, and what an aggregator (PM) actually can —
  and cannot — verify about reports from team members.
---

# Self-Audit Tools Skill

When a task asks you to audit your own toolset, follow this protocol exactly.
This skill is the **single source of truth** for self-audit; the task brief
will not duplicate these rules, so read this in full before acting.

## Capability honesty — read this first

This skill is written around what each role can actually observe:

| Role | Can observe | Cannot observe |
|---|---|---|
| Member (audit subject) | Own tool catalog, own tool-call results, own message history | Other members' tools, other members' message history |
| Aggregator (orchestrator / PM) | Emails received, meeting transcripts, `agent_spawner__get_team_status` (high-level state per member) **AND its own toolset** | Members' tool catalogs, members' tool-call counts, anything not explicitly emailed or said in a meeting |
| Requester (Jarvis / user) | Whatever the aggregator delivers | Same blind spots as the aggregator |

That table is load-bearing. Rules below honour it — the aggregator never
"verifies counts" of another member because it cannot. The aggregator only
checks what is plainly present in the text it receives.

## Which role are you?

Answer this **first**, in your own head, before reading further:

- **No team around you** (solo spawn, no PM, no peers in roster). You are
  a *standalone member*. Run Part A and email the report to the
  requester named in your context (Jarvis / parent agent). Part B does
  not apply.
- **You are a team member** (BA / SA / Dev / Designer / QE / DSO / etc.,
  with a PM in your roster). Run Part A and email your report to the
  PM. Do not run Part B — only the orchestrator aggregates.
- **You are the orchestrator** (PM, role tagged `orchestrator` in the
  template, or first spawned by `spawn_team_tool`). Run **both**
  parts: Part A for your own toolset (no email — you are the
  recipient), then Part B to collect member emails and produce the
  combined roll-up that interleaves your own audit with theirs.

Re-read the table above and the bullet list whenever you forget which
half of the skill applies to you.

## TL;DR (read first, act second)

**As a member doing the audit:**

1. Enumerate **every** tool you can see — not just the ones you happened to use.
2. Classify each: **READ** (safe to call) vs **WRITE/DESTRUCTIVE** (do NOT call).
3. For READ tools: call once with the safest minimal valid args. Record the
   outcome verbatim.
4. For WRITE/DESTRUCTIVE tools: do NOT call. Record purpose + the call shape
   you would use, based on the tool's schema.
5. Email the markdown report to the requester (usually PM). Use a meeting
   only to point at the email — never paste the table into a transcript.
6. Even if early steps fail (turn errors, ACK errors, etc.) — keep going.
   The audit deliverable is the inventory, not the meeting choreography.

**As an orchestrator (PM) running an audit round:**

7. Do Part A on **your own** toolset first. Keep your inventory table
   in memory (or save it as a workspace draft) — you do NOT email
   yourself, you do NOT speak it in a meeting. Your audit becomes the
   first entry in the roll-up at Step 10.
8. Wait until `agent_spawner__get_team_status` shows all members done,
   or a sane deadline. Collect the inbox.
9. For each report received, do only the checks plainly observable in
   the text: structure present, summary internally reconciles, table
   non-empty.
10. Concatenate **your own** audit and every member's report verbatim
    under a single header. Name who replied and who did not. Do NOT
    invent counts or thresholds across members.

## When to apply

- Brief contains "audit your tools" / "self-audit" / "list and verify tools".
- You are asked to characterise your own capabilities.
- This skill is **only** about *your own* tools. Do not audit other agents.

---

# Part A — Member protocol

## Step 1 — Enumerate your tools

You see every tool you can call (the tool catalog is given in your
system context). Do not guess from role title; enumerate. List every
tool with full canonical name (e.g. `filesystem__list_directory`,
`github__search_repositories`, `meeting_room__speak`).

**Anti-pattern:** reporting only the 2-3 tools you happened to use for
housekeeping (ACK, leave_meeting, get_meeting_status) and calling that an
audit. That is the meeting trace, not a tool audit.

## Step 2 — Classify each tool: READ vs WRITE/DESTRUCTIVE

Rule of thumb: **does calling the tool change observable state anywhere**
(filesystem, database, external API, queue, agent memory, other agent's
inbox)?

| Class | Definition | Examples |
|---|---|---|
| **READ** | Returns data, no side effect | `*__list_*`, `*__get_*`, `*__search_*`, `*__read_*`, `*__status`, `*_info`, `execute(git log/status/diff)`, `execute(gh pr view/list)`, `jira_get_issue`, `confluence_search`, `github__list_commits` |
| **WRITE** | Creates/modifies state | `*__create_*`, `*__update_*`, `*__edit_*`, `*__write_*`, `*__send_*`, `jira_transition_issue`, `github__create_pull_request` |
| **DESTRUCTIVE** | Deletes or otherwise non-reversible | `*__delete_*`, `git_reset --hard`, `execute(rm ...)`, `jira_delete_issue` |

If the schema description is ambiguous, default to **WRITE** (safer).
Tools named `get_*` / `list_*` / `status` are strong READ signals even
if the description is vague.

**The communication-tools paradox.** `email__send_email` and
`meeting_room__speak` are WRITE-class — they emit observable messages.
This does NOT mean "you cannot use them". It means: **do not call them
as dummy audit tests** (no "audit test email", no "audit test speak").
You may — and must — use them for their primary purpose: delivering
the report and acknowledging meeting turns. Record them as
`status=skipped, note="skipped: write — used for audit delivery, not test-called"`.

## Step 3 — Test READ tools

Call each READ tool exactly once with the safest minimal valid arguments.

### Argument hygiene

- Use a known-existing benign resource (e.g. `github.com/octocat/Hello-World`,
  `https://example.com`).
- Use `limit=1` / `per_page=1` / `start_at=0` to keep results tiny.
- For filesystem tools, restrict to a directory you are explicitly allowed.
  If unsure and the tool exists, call `filesystem__list_allowed_directories`
  first to learn the safe roots.
- Never use real production identifiers (a customer's email, an unrelated
  Jira key).

### Timeouts and hangs

If a tool call does not return within ~30 seconds, stop waiting. Mark
`status=error, note="timeout (>30s)"` and move on. One hung tool must
not block the rest of the audit.

### Restrictions like "do not read source"

If your brief says "do not read source code of this repo to do the audit",
that means: do not USE file reads as the AUDIT METHOD (inferring tool
behaviour from source). It does **NOT** mean "skip every filesystem
tool from the inventory". Still test the filesystem READ tools with a
benign path (allowed workspace, `/tmp`). Only skip if the brief
literally names a tool.

### Recording the outcome

For each READ call:
- `status` = `ok` | `error`
- `note` = the verbatim error message, or empty
- If the error is your own bad args (validation, not-found), the *tool
  itself* worked — mark `status=ok, note="schema works; got expected
  validation error for synthetic args"`. Reserve `status=error` for the
  tool genuinely failing (rate limit, server down, auth missing, timeout).

## Step 4 — Document WRITE/DESTRUCTIVE tools without calling

For each WRITE/DESTRUCTIVE tool:
- `status` = `skipped`
- `note` = one-line description of what the tool would do + the call shape.

Example: `skipped: opens a PR against a remote repo; usage: github__create_pull_request({owner, repo, title, head, base, body})`.

## Step 5 — Exchange format: markdown, never JSON

### Per-member report (your email body)

```markdown
# Self-audit — <Your Name>

Summary: **N** tools — **X** ok / **Y** skipped (write/destructive) / **Z** error.

Top errors (omit this section if Z == 0):
- `<tool name>` — <one-line reason>

## Tool inventory

| # | Tool | Class | Status | Note / Usage |
|---:|---|---|---|---|
| 1 | filesystem__list_directory | READ | ok |  |
| 2 | filesystem__write_file | WRITE | skipped | usage: `filesystem__write_file({path, content})` |
| 3 | github__search_repositories | READ | ok |  |
| 4 | github__create_pull_request | WRITE | skipped | usage: `github__create_pull_request({owner, repo, title, head, base, body})` |
| 5 | email__send_email | WRITE | skipped | used for audit delivery, not test-called |
```

(The numbers above are an illustrative shape, not a target — your real
counts come from your real tools.)

### Table rules

- One row per tool, one tool per row. Do not collapse families.
- `Tool` = full canonical name `<server>__<tool>`.
- `Class` ∈ {READ, WRITE, DESTRUCTIVE}.
- `Status` ∈ {ok, skipped, error}.
- `Note / Usage` is plain text or inline code; no line breaks in a cell
  (use ` — ` or `; `).
- **Self-reconcile before sending:** `X + Y + Z` MUST equal `N`. This is the
  only arithmetic check downstream (PM) can perform on your report, so
  do it yourself first.

### Sending — depends on your role

| Your role | Action |
|---|---|
| **Team member** (BA / SA / Dev / QE / Designer / DSO …) | Email the markdown block to the orchestrator (PM). See template below. |
| **Standalone audit subject** (no PM in roster) | Email the markdown block to the spawner / parent agent named in your context (Jarvis / user-facing agent). Your email IS the deliverable; there is no roll-up step. |
| **Orchestrator** (PM or first-spawned) | Do NOT send an email. Keep this markdown block ready — it goes inline at the top of the Step 10 roll-up, before the member reports you collect. |

For the two email-sending cases:

```
email__send_email(
  to       = "<requester name>",   # PM if you are a member; Jarvis / parent if standalone
  subject  = "[AUDIT] <your name> self-audit complete",
  body     = "<the markdown block above, verbatim>",
  priority = "normal",
)
```

The orchestrator MUST NOT email itself, post the table into a meeting,
or write it to a workspace file as a final deliverable — those are
indirection paths that confuse the roll-up. The orchestrator's audit
lives in memory (or as a workspace draft) until Step 10 concatenates
it with the member reports.

### In a concurrent meeting

If a meeting is in flight (e.g. sprint review), say exactly ONE sentence
when it is your turn:

> "Self-audit complete; full report emailed to <recipient>. Summary: N tools, X ok / Y skipped / Z error."

Do not paste the table. Do not narrate findings. The meeting is for
discussion, not bulk data.

## Step 6 — When the meeting flow misbehaves, keep auditing

Common failures during a self-audit kickoff:

| Symptom | Reason | Do this |
|---|---|---|
| `speak` → "Not your turn. Current speaker: X" | Race — multiple members tried to ACK at once | Wait for `MEETING_TURN` signal in your inbox; on your turn, ACK then continue. Do NOT abandon the audit. |
| `leave_meeting` → "Cannot leave_meeting while it's your turn" | You are the current speaker | Call `speak` (or `skip_turn`) first; that advances the turn. THEN leave. |
| Kickoff times out before your ACK | `max_rounds` reached | Skip the ACK — it's housekeeping. Continue Steps 1–5 anyway and email the report. |

The audit deliverable (the inventory table) is what matters. Do not
conflate "I had trouble with meeting_room" with "I couldn't do the audit".

## Step 7 — Re-runs

If the audit is re-triggered (user asks again, PM re-spawns), your new
email REPLACES the previous one. Do not concatenate to or amend the old
report — the aggregator will use the latest email per sender.

---

# Part B — Orchestrator / aggregator protocol

This part is for the orchestrator (typically the PM in an agile team,
or any agent marked `orchestrator` in the template). If you are a
member and not the orchestrator, **skip this part entirely** — your
deliverable is the email from Part A.

**Before reading Step 8 onwards, run Part A on your own toolset.** You
are both an audit subject and the aggregator. Keep your own inventory
in scratch memory (or as a workspace draft markdown file); it goes
inline at the head of the roll-up in Step 10. Do NOT send the
`[AUDIT]` email to yourself — that would be a delivery indirection
with no recipient.

## What you can and cannot do (recap)

You CAN see, plainly, in your own inbox / meeting transcripts:
- Whether an email arrived from each member.
- The body text of each email — including the markdown table.
- The high-level state of each member via `agent_spawner__get_team_status`
  (running / idle / completed / error — not tool details).

You CANNOT see:
- Any member's tool catalog.
- Any member's tool-call history.
- The "right" number of tools any member should have audited.

Therefore your verdict is about **what arrived and whether the text
itself is well-formed**, not about whether the numbers are objectively
correct. There is no ground truth available to you. Do not invent one.

## Step 8 — Wait for reports

1. Note the spawned member list (from your own spawn call or
   `agent_spawner__get_team_status`).
2. Wait until `agent_spawner__get_team_status` reports every member as
   `idle` / `completed` / `error`, OR a reasonable deadline you choose
   for the task (e.g. 5 minutes after spawn if the audit is short).
3. Read your inbox. Collect all `[AUDIT]`-tagged emails.

If a member never replies and never goes terminal, name them in the
roll-up as "no report received" rather than waiting forever.

## Step 9 — Per-report sanity checks (text-level only)

For each email received, check ONLY what is visible in the text:

| Check | Pass condition | How to apply |
|---|---|---|
| **Structure present** | Email body has a `## Tool inventory` heading and a markdown table under it. | Plain string scan. |
| **Table non-trivial** | The table has more than a handful of rows AND lists at least one tool whose canonical name does NOT start with `meeting_room__` or `email__`. | Plain scan. A 3-row table consisting only of meeting/email tools means the member did housekeeping and called it an audit. |
| **Internal reconcile** | The member's own summary `X + Y + Z` equals their own `N`. | Arithmetic on numbers they themselves wrote. |

These three checks are the only verdict criteria. You are not checking
whether the agent's count is "right" — you cannot. You are checking
that the report is shaped like an audit report and adds up to itself.

## Step 10 — Produce the roll-up

Emit ONE markdown document to the requester (Jarvis / user). The
roll-up has FOUR pieces — your own audit is the first; member emails
follow verbatim:

```markdown
# Self-audit — Team Roll-up

Submissions: K of M (including yourself).
- Replied: <list of agent names, with you marked "(orchestrator, inline)">
- Did not reply: <list of agent names, or "none">

Verdict: **PASS** / **PARTIAL** / **FAIL** — <one-sentence rationale>

## Self-audit — <Your name> (orchestrator)

<paste your own Part A markdown block verbatim — same shape as a
member email body. No "[AUDIT]" subject line; you didn't email it.>

## Per-member reports

<for each replied member, paste their email body verbatim, prefixed by a `---` separator>

## Notes (optional)

<free-text observations you can plainly support from the text — e.g.
"Member X listed 3 tools and all were meeting_room — likely did not
enumerate properly". Skip this section if nothing notable.>
```

Count yourself in the `K of M` denominator and numerator. The Step 9
checks (structure / non-trivial / internal reconcile) apply to your
own audit too — apply them honestly to your own block before
declaring the verdict.

### Verdict definition (text-level only)

- **PASS** if every spawned member replied AND every reply passed all
  three Step 9 checks.
- **PARTIAL** if some replied with passing reports and some did not
  reply or replied with malformed/trivial reports. Name the gaps in
  the rationale.
- **FAIL** if the majority of members did not reply or replied with
  malformed reports.

You do NOT compute numeric thresholds across members. You do NOT
compare any member's count to an expected toolset size. You only
report what is plainly there.

## Step 11 — Deliver and stop

Send the roll-up to the requester. Do not start a new round of audit
unless explicitly asked.

---

# Anti-patterns (applies to both roles)

- **Member: Reporting 3 tools and calling it done.** Listing only the
  tools you happened to use for ACK + leave is not an audit. Enumerate
  everything.
- **Member: Sending JSON instead of markdown.** Use the markdown table.
- **Member: Pasting the table into a meeting.** Use email; in the meeting,
  one sentence pointer.
- **Member: Skipping a whole tool family because of a vague rule.**
  "Do not read files" does not mean skipping `list_directory` /
  `get_file_info` / `search_files`. Re-read Step 3.
- **Member: Test-calling WRITE tools "just to check".** Do not.
  Describe them. The audit-delivery email is the only WRITE call you
  are required to make.
- **Member: Marking `error` for your own bad args.** That is `ok`
  (schema works) — see Step 3.
- **Member: Abandoning audit because meeting turns misbehaved.** The
  audit is the deliverable; meeting choreography is noise.
- **PM / Orchestrator: Inventing counts or thresholds across members.**
  You have no ground truth for any member's full toolset. Step 9 is
  your entire verdict toolbox.
- **PM / Orchestrator: Rubber-stamping PASS just because emails arrived.**
  Apply the three Step 9 checks. A 3-row meeting-only table is FAIL.
- **PM / Orchestrator: Re-computing or "correcting" a member's numbers.**
  Their table, their numbers. You report them verbatim.
- **PM / Orchestrator: Waiting indefinitely for a non-responder.**
  Mark them as "no report received" and ship the partial roll-up.
- **PM / Orchestrator: Skipping your own audit.** You are also an audit
  subject. Run Part A on yourself, inline the result in Step 10. Do
  NOT email yourself, do NOT list yourself as "did not reply" because
  you didn't email.
- **PM / Orchestrator: Emailing yourself the audit.** Self-addressed
  emails clutter the inbox and confuse the Step 9 reconcile. Your
  audit lives inline in the roll-up; the inbox is for member emails
  only.

# Definition of done

**Member is done when:**
1. Enumerated every tool visible.
2. Tested every READ tool exactly once (or marked timeout).
3. Recorded every WRITE/DESTRUCTIVE tool with `skipped` + usage shape.
4. Summary `X + Y + Z` equals `N` (self-reconcile).
5. Emailed the markdown report to the requester.
6. (If in a meeting) posted a one-sentence pointer to the email.

**Orchestrator (PM) is done when:**
1. Ran Part A on own toolset — own inventory block ready (not emailed).
2. Every spawned member is terminal or the deadline passed.
3. Each received email passed (or failed) Step 9 checks.
4. The roll-up document was delivered to the requester with Submissions
   (counting self), Verdict, the orchestrator's own inventory inline,
   and verbatim per-member reports.
5. No counts were invented across members; no member's numbers were
   altered.

If any step is missing, say so honestly. Do not declare PASS to close
the loop.
