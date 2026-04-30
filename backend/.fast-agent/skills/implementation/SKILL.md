---
name: implementation
description: Guidelines for implementing features and fixes. Covers code quality, file organization, and the implementation workflow within the team context.
---

# Implementation Guide (DEV)

Structured approach to implementing features and bug fixes within a multi-agent team.

## Implementation Workflow

### 1. Understand the Task
- Read the requirements from PM's instructions or meeting context
- Check existing code in the workspace
- Identify affected files and components

### 2. Plan Before Coding
- Break down the task into small, verifiable steps
- Identify dependencies and potential conflicts
- Determine the simplest approach that meets requirements

### 3. Implement
- Write clean, self-documenting code
- Follow existing project conventions and patterns
- Keep changes focused — one concern per commit
- Add error handling from the start

### 4. Verify Your Work
- Run existing tests to check for regressions
- Test your changes manually if applicable
- Ensure code is syntactically correct (no import errors, no typos)

### 5. Report
- Summarize what you changed and why
- List files modified
- Note any concerns or trade-offs

## Code Quality Standards

- **Early returns** over nested conditions
- **Descriptive names** — avoid `utils`, `helpers`, `misc`
- **Small functions** — under 50 lines when possible
- **Small files** — under 200 lines when possible
- **DRY** — extract common logic into reusable functions
- **Error handling** — validate inputs, handle edge cases
- **Comments** — explain "why", not "what"

## In Meeting Context

When working in a meeting:
- Use `filesystem` tools to read/write code during your turns
- Show concrete changes in your `speak()` responses
- If QE gives a FAIL verdict, fix the issues immediately using your tools
- Don't just describe what you'll do — actually do it during the meeting
