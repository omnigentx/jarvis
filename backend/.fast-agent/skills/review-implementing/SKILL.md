---
name: review-implementing
description: Process and implement code review feedback systematically. Use when receiving review comments or FAIL verdicts and needing to address them.
---

# Implementing Review Feedback

Adapted from [mhattingpete/claude-skills-marketplace](https://github.com/mhattingpete/claude-skills-marketplace/tree/main/engineering-workflow-plugin/skills/review-implementing).

## When to Use

- After receiving a FAIL verdict in a meeting
- When reviewer lists issues to address
- When code review comments need to be implemented

## Systematic Workflow

### 1. Parse Feedback
- Extract individual issues from the review
- Categorize: Critical → Important → Minor
- Clarify ambiguous items before starting

### 2. Prioritize
Fix in order:
1. **Critical** — Must fix before anything else
2. **Important** — Fix before requesting re-review
3. **Minor** — Fix if time permits, or note for later

### 3. Fix Each Issue
For each item:
1. **Locate** the relevant code
2. **Understand** the reviewer's intent
3. **Make the change** — minimal, focused fix
4. **Verify** — check syntax, run relevant tests
5. **Report** — show what you changed in your `speak()`

### 4. Request Re-Review
After fixing Critical and Important issues:
- Summarize what you fixed
- Show test results
- Ask reviewer to re-check

## Handling Edge Cases

**Conflicting feedback:**
- Ask PM for guidance in the meeting

**Breaking changes required:**
- Notify team before implementing

**Tests fail after fix:**
- Fix tests before reporting done

**Can't reproduce the issue:**
- Ask reviewer for more details
