---
name: qe-workflow
description: >
  QE workflow for test strategy, test fixing, code review, and quality verdicts.
  Use when QE needs to validate code, write test plans, or report quality assessment.
---

# QE Workflow

## Testing Skills

| Type | Skill | When to use |
|------|-------|-------------|
| Frontend E2E | `playwright-skill` | UI testing, browser automation, responsive checks |
| Backend API | `api-testing` | Endpoint testing, API contracts, mock servers |

Read the appropriate skill BEFORE writing tests.

## Your Process

1. **Read BRD** → understand requirements and acceptance criteria
2. **Read Dev's code** → review for correctness, quality, edge cases
3. **Write test plan** → systematic coverage of happy path + edge cases
4. **Execute tests** → run test suite, analyze failures
5. **Report verdict** → PASS or FAIL with evidence

## Output Rule — MANDATORY

| Deliverable | Destination | Tool |
|------------|-------------|------|
| Test Plan | Confluence page | `confluence_create_page` |
| Bug Reports | Jira issues (type: Bug) | `jira_create_issue` |
| Workspace files | Temporary drafts ONLY | `write_file` |

## Test Plan Template

Publish to Confluence (NOT workspace MD file):

```markdown
# Test Plan

| ID | Scenario | Steps | Expected Result | Status |
|----|----------|-------|-----------------|--------|
| TC-1 | Happy path: ... | 1. ... | ... | PASS/FAIL |
| TC-2 | Edge case: ... | 1. ... | ... | PASS/FAIL |
```

## Fixing Failing Tests

When tests fail, fix systematically:
1. **Group errors** by type (ImportError, AssertionError, etc.)
2. **Fix infrastructure first** (imports, deps, config)
3. **Then API changes** (signatures, renames)
4. **Finally logic** (assertions, business rules)
5. Run subset tests after each fix group

## Verdict Format

Always end reviews with:
```
[DECISION] VERDICT: PASS — <reason>
[DECISION] VERDICT: FAIL — <key issues>
```

## Bug Report Template

```markdown
## Bug: [Title]
- **Severity**: Critical / Major / Minor
- **Steps to Reproduce**: 1. ... 2. ...
- **Expected**: ...
- **Actual**: ...
- **Evidence**: [file:line, screenshot, or log]
```

## References

| Topic | File |
|-------|------|
| Code review protocol | [CODE_REVIEW.md](references/CODE_REVIEW.md) |
| Terminal execution | [TERMINAL.md](references/TERMINAL.md) |
| Meeting protocol | [MEETING_PROTOCOL.md](references/MEETING_PROTOCOL.md) |
| Jira issue tracking | [JIRA_TRACKING.md](references/JIRA_TRACKING.md) |
