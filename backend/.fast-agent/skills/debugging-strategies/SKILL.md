---
name: debugging-strategies
description: >
  Systematic debugging framework. Use when a developer agent needs to
  find the root cause of a bug, debug performance issues, or analyse a
  production failure.
---

# SYSTEMATIC DEBUGGING

## 4-phase process

### Phase 1: REPRODUCE
- Confirm the bug exists.
- Record the exact steps to reproduce.
- Capture: error message, stack trace, logs.

### Phase 2: HYPOTHESIZE
- Read the error message carefully — it usually contains a clue.
- List 2–3 of the most likely hypotheses.
- Rank them by likelihood.

### Phase 3: TEST
- Test the highest-likelihood hypothesis FIRST.
- Binary search: split the code in half, test each half.
- Add temporary logging at suspect points.
- DO NOT change code before you understand the root cause.

### Phase 4: FIX & VERIFY
- Fix the root cause, NOT the symptom.
- Write a test that reproduces the bug BEFORE the fix.
- Verify the fix: the new test must pass.
- Remove temporary logging.

## Decision tree

```
What kind of bug?
├── Runtime error → Read the stack trace; locate the failing line.
├── Logic error → Add print/log at input and output points.
├── Performance → Profile: measure time spent per phase.
├── Intermittent → Look for race conditions; check timing.
└── Hard to reproduce → Add defensive logging.
```

## ❌ Anti-patterns
- Guess → patch random code → hope it works.
- Fix the symptom → root cause stays → bug returns.
- Skip writing a test → same bug recurs after a future refactor.

## ✅ Standard
- Reproduce → Hypothesis → Test → Fix → Verify → Commit with test.
