---
name: test-driven-development
description: Write tests first, watch them fail, then write minimal code to pass. Use for all new features and bug fixes.
---

# Test-Driven Development (TDD)

Adapted from [obra/superpowers](https://github.com/obra/superpowers/tree/main/skills/test-driven-development).

## Core Rule

```
NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST
```

If you wrote code before the test — delete it. Start over. No exceptions.

## Red-Green-Refactor Cycle

### RED — Write Failing Test
Write ONE minimal test showing what should happen.
- Clear, descriptive test name
- Tests real behavior, not mocks
- One assertion per test

### GREEN — Minimal Code to Pass
Write the **simplest** code that makes the test pass.
- No over-engineering
- No "future-proofing"
- Just enough to go green

### REFACTOR — Clean Up
Improve code quality while keeping tests green.
- Extract common patterns
- Improve naming
- Remove duplication

## When to Use

**Always:**
- New features
- Bug fixes
- Behavior changes

**Exceptions (discuss with team first):**
- Throwaway prototypes
- Configuration files
- Generated code

## Anti-Patterns

- ❌ Writing tests after implementation
- ❌ Keeping "reference" code you wrote before tests
- ❌ Over-engineering with options/config nobody asked for (YAGNI)
- ❌ Testing mock behavior instead of real behavior
- ❌ Skipping the "watch it fail" step

## In Meeting Context

When implementing with TDD in a meeting:
1. Write the test first — show it in your `speak()`
2. Run it — show it fails
3. Write minimal implementation
4. Run it — show it passes
5. Report results to QE for review
