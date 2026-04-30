---
name: verification-before-completion
description: >
  Completion checklist for any task. Use when QE needs to verify fixes,
  or any role needs to ensure a task is truly complete.
---

# VERIFICATION BEFORE COMPLETION

<verification_checklist>
## BEFORE reporting "done", verify ALL of the following:

### Code Quality
- [ ] Code runs without errors
- [ ] No unresolved TODO/FIXME
- [ ] No leftover console.log/print debug statements
- [ ] Code is properly formatted

### Testing
- [ ] Unit tests pass
- [ ] Edge cases are covered
- [ ] Test failure messages are clear and descriptive

### Documentation
- [ ] Docstrings for new functions
- [ ] README updated if setup changed
- [ ] Inline comments for complex logic

### Integration
- [ ] Does not break existing functionality
- [ ] API contracts match spec
- [ ] UI renders correctly across screen sizes
</verification_checklist>

<bugfix_protocol>
When fixing a bug:
1. Reproduce the bug (you MUST see it happen)
2. Apply the fix
3. Verify the bug NO LONGER occurs
4. Run the ENTIRE test suite (not just related tests)
</bugfix_protocol>

<violation>
- Reporting "done" without running the code → VIOLATION
- Only testing the happy path → VIOLATION
- Saying "should work" without verification → VIOLATION
</violation>

## ✅ Correct
- "Verified: tests pass, edge case X handled, no regressions"
