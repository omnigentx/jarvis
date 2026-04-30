---
name: git-workflow
description: >
  Git workflow và conventions. Dùng khi Dev hoặc DSO cần commit, branch,
  hoặc tạo pull request theo chuẩn.
---

# GIT WORKFLOW

## Branch Strategy

```
main (production)
├── develop (staging)
│   ├── feature/add-dark-mode
│   ├── feature/new-agent-card
│   ├── bugfix/fix-calendar-dup
│   └── hotfix/fix-auth-crash
```

## Branch Naming
- `feature/<short-description>` — new feature
- `bugfix/<short-description>` — bug fix
- `hotfix/<short-description>` — urgent prod fix

## Commit Conventions

```
<type>(<scope>): <description>

Types: feat, fix, docs, refactor, test, chore
Scope: backend, frontend, skills, infra
```

Ví dụ:
- `feat(backend): add finance decision tree skill`
- `fix(frontend): fix agent card overflow on mobile`
- `refactor(skills): merge system-design into architecture`

## Pull Request Template

```markdown
## What
[Mô tả ngắn]

## Why
[Lý do thay đổi]

## How
[Approach/Technical details]

## Testing
- [ ] Unit tests pass
- [ ] Manual testing done

## Screenshots (nếu UI)
```

## Quy tắc
- Commit thường xuyên, message rõ ràng
- 1 PR = 1 feature/bugfix
- Rebase trước khi merge (không merge commits)
- KHÔNG push trực tiếp lên main
