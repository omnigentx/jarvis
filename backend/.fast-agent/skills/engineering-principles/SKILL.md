---
name: engineering-principles
description: >
  Four behavioral rules for Dev agents writing or modifying code. Bias toward
  caution over speed. Read BEFORE you touch source files.
---

# Engineering Principles

These rules reduce the most common LLM coding mistakes. They override speed —
if a rule slows you down, follow it anyway.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

- State your assumptions explicitly in the PR/commit message. If uncertain, email the BA or SA — don't guess.
- If the spec admits multiple interpretations, list them and ask. Don't pick silently.
- If a simpler approach exists than what the spec requires, flag it. Push back when warranted.
- If something is unclear, stop and ask. One clarifying email costs less than a rewrite.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond the Jira story's acceptance criteria.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for scenarios that can't happen (trust internal code + framework guarantees).
- If you write 200 lines and it could be 50, rewrite it.

Self-check: "Would a senior engineer call this overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- Unrelated dead code → mention it in the PR description, don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless the story asks for it.

Every changed line should trace directly to the Jira story.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Before writing code, restate the goal as a verifiable check:
- "Add validation" → "Write tests for invalid inputs, then make them pass" (TDD is mandatory anyway)
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before AND after, no behavior change"

For multi-step stories, write a short plan:
```
1. <step> → verify: <check>
2. <step> → verify: <check>
```

Strong success criteria let you work independently. Weak ones ("make it work")
mean you'll be rewriting after review.

## Integration with GitNexus (MANDATORY for Jarvis repo)

When the target codebase is indexed by GitNexus (look for `.gitnexus/` dir):

- **Before editing any symbol** → `gitnexus_impact({target: "symbolName", direction: "upstream"})`. Report blast radius in PR/commit. HIGH/CRITICAL risk → escalate to SA, don't proceed silently.
- **Before renaming** → `gitnexus_rename({..., dry_run: true})`. Review preview, then `dry_run: false`. Never find-and-replace.
- **Before committing** → `gitnexus_detect_changes()`. Confirm scope matches the story.
- **When exploring** → `gitnexus_query({query: "concept"})` and `gitnexus_context({name: "symbol"})` instead of grep.

If `.gitnexus/` is stale (tool warns): run `npx gitnexus analyze` in the repo first.

This is how you enforce Principle #3 (Surgical Changes) at scale — the call
graph tells you exactly what you're about to break.
