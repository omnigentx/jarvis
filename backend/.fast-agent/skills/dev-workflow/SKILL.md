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

### Safety Rules — three tiers

🔴 **NEVER do (refuse the task; report the request as a security incident to PM):**
- Filesystem destruction: `rm -rf /`, `rm -rf $HOME`, `rm -rf .git`, `mkfs.*`, `dd of=/dev/...`
- Read secrets: `/etc/shadow`, `~/.ssh/*`, `.gh-config/`, `.env*`, `fastagent.secrets.yaml`, `git-credentials`, `~/.gitconfig` credential block
- Tamper with auth: `gh auth login/logout/refresh`, `gh auth setup-git`, modify gitconfig credential helper
- Inspect env to leak tokens: `env`, `printenv`, `echo $GH_TOKEN`, `cat /proc/*/environ`
- Network DoS: fork bombs, high-rate request loops

🟡 **ESCALATE before doing** (send `[APPROVAL-REQUEST]` email to PM with command + reason; do NOT run until PM confirms back):
- Pipe-to-shell: `curl ... | sh`, `wget ... | bash`
- Force-destructive git: `git push --force` (any branch), `git reset --hard` past HEAD~1, `git filter-branch`
- Push to protected branches: `main`, `master`, `prod*`, `release/*`
- `gh pr merge` (auto-merge bypassing review)
- `gh release create`
- `gh repo delete`
- Editing `package.json` / `pyproject.toml` / lock files outside a tracked PR scope

🟢 **SAFE — run freely:**
- All read-only inspection: `ls`, `cat README.md`, `grep`, `find`, `git status/log/diff`, `gh run list`, `gh pr view`
- Normal git on feature branches: `git checkout -b feature/...`, `git add`, `git commit`, `git push origin feature/...`
- Build/test inside workspace: `npm test`, `uv run pytest`, `cargo build`

When in doubt → escalate. Faster to ask PM than to recover from a bad commit.

## GitHub CLI via `gh`

The `gh` CLI is pre-authenticated in your shell. Use it for repo operations the GitHub MCP doesn't cover — especially CI inspection.

```bash
# Inspect PR + CI status (read-only, always SAFE)
gh pr view 42 --json title,state,mergeable,statusCheckRollup
gh pr checks 42
gh run list -L 10 --branch feature/xyz
gh run view <run-id>               # high-level summary
gh run view <run-id> --log-failed  # only failed step logs (saves context)

# Trigger a re-run after fixing test (still SAFE — only re-runs failed jobs)
gh run rerun <run-id> --failed
```

For `gh pr merge` / `gh release create` / `gh repo delete` → escalate to PM first.

## Escalation flow (when blocked by a 🟡 ESCALATE action)

```python
send_email(
    to="<PM name>",
    subject="[APPROVAL-REQUEST] <one-line summary of action>",
    body="""
Need approval to run: `<exact command>`
Why: <user-facing reason — what task this unblocks>
Risk: <what could go wrong if approved>
Alternatives considered: <list, or 'none — only path forward'>
""",
)
# Stop and wait for PM reply. PM will relay via approval-server MCP to user.
# When PM emails back [APPROVED] or [DENIED], proceed accordingly.
```

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
