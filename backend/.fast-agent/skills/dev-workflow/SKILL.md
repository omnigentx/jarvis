---
name: dev-workflow
description: >
  Developer workflow covering terminal execution, TDD cycle, code review, and git.
  Use when Dev needs to implement features, run tests, or submit code for review.
---

# Dev Workflow

## Terminal Execution

Use the `execute` tool to run shell commands in your workspace:

```
execute(command="<shell command>")
```

**Key characteristics:**
- Runs in **workspace directory**
- **90s timeout** — killed if no output for 90s
- Each call = **separate shell session** (env vars / `cd` don't persist)
- Output **auto-truncated** if too long

### Efficient Patterns
```bash
cd repo && npm install && npm test        # Sequential (stops on error)
cd backend && uv run pytest --tb=short -q 2>&1 | tail -20  # Filter output
```

### Safety Rules
- 🔴 NEVER: `rm -rf /`, expose secrets, `curl | bash` from untrusted sources
- 🟡 CAUTION: `rm -rf` → always specify exact path
- 🟢 SAFE: all read-only commands, build/test within workspace

## TDD Cycle

```
NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST
```

1. **RED** — Write ONE minimal failing test
2. **GREEN** — Write simplest code to pass
3. **REFACTOR** — Clean up while keeping green

## Deliverable Flow

1. Clone repo → `execute(command="git clone <url> repo")`
2. Create feature branch → `execute(command="cd repo && git checkout -b feature/...")`
3. Write tests → implement → verify
4. Commit + push → create PR via `github` tools
5. Email reviewer: `send_email(to="Tuan - QE", body="PR ready for review")`

## Error Handling

When a command fails:
1. Read stderr FIRST
2. DO NOT retry immediately — analyze first
3. Try a simpler command — isolate the issue
4. Report clearly — include command + error output

## References

| Topic | File |
|-------|------|
| Code review protocol | [CODE_REVIEW.md](references/CODE_REVIEW.md) |
| TDD detailed guide | [TDD.md](references/TDD.md) |
| Git branching & conventions | [GIT_WORKFLOW.md](references/GIT_WORKFLOW.md) |
| Meeting protocol | [MEETING_PROTOCOL.md](references/MEETING_PROTOCOL.md) |
| Jira issue tracking | [JIRA_TRACKING.md](references/JIRA_TRACKING.md) |
