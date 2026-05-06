---
name: git-workflow
description: >
  Git workflow and conventions. Use when a Dev or DSO needs to commit,
  branch, or open a pull request following the team standard.
---

# GIT WORKFLOW

## Branch strategy

```
main (production)
├── develop (staging)
│   ├── feature/add-dark-mode
│   ├── feature/new-agent-card
│   ├── bugfix/fix-calendar-dup
│   └── hotfix/fix-auth-crash
```

## Branch naming
- `feature/<short-description>` — new feature
- `bugfix/<short-description>` — bug fix
- `hotfix/<short-description>` — urgent prod fix

## Commit conventions

```
<type>(<scope>): <description>

Types: feat, fix, docs, refactor, test, chore
Scope: backend, frontend, skills, infra
```

Examples:
- `feat(backend): add finance decision tree skill`
- `fix(frontend): fix agent card overflow on mobile`
- `refactor(skills): merge system-design into architecture`

## Pull request template

```markdown
## What
[Short description]

## Why
[Reason for the change]

## How
[Approach / technical details]

## Testing
- [ ] Unit tests pass
- [ ] Manual testing done

## Screenshots (if UI)
```

## Rules
- Commit often, with clear messages.
- One PR = one feature or bug fix.
- Rebase before merging (no merge commits).
- DO NOT push directly to main.
