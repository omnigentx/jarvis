---
name: test-fixing
description: Systematically identify and fix failing tests using smart error grouping. Use when tests are failing and need to be fixed.
---

# Test Fixing

Adapted from [mhattingpete/claude-skills-marketplace](https://github.com/mhattingpete/claude-skills-marketplace/tree/main/engineering-workflow-plugin/skills/test-fixing).

## When to Use

- Tests are failing after implementation changes
- CI/CD reports test failures
- QE reports FAIL verdict with test failures

## Systematic Approach

### 1. Run All Tests
Run the test suite to see the full picture:
- Total number of failures
- Error types and patterns
- Affected modules/files

### 2. Group Errors

Group similar failures by:
- **Error type**: ImportError, AttributeError, AssertionError, etc.
- **Module/file**: Same file causing multiple failures
- **Root cause**: Missing deps, API changes, refactoring impacts

### 3. Fix in Priority Order

**Infrastructure first:**
1. Import errors
2. Missing dependencies
3. Configuration issues

**Then API changes:**
4. Function signature changes
5. Module reorganization
6. Renamed variables/functions

**Finally logic issues:**
7. Assertion failures
8. Business logic bugs
9. Edge case handling

### 4. Fix One Group at a Time

For each group:
1. **Identify root cause** — Read code, check recent changes
2. **Implement fix** — Minimal, focused changes
3. **Run subset tests** — Verify this group passes
4. **Move to next group** — Only after current passes

### 5. Final Verification

After all groups fixed:
- Run complete test suite
- Verify no regressions
- Report results

## Best Practices

- Fix one group at a time
- Run focused tests after each fix (`pytest tests/specific_test.py -v`)
- Keep changes minimal
- Don't move to next group until current passes
- Look for patterns — one fix might solve multiple failures
