# Delegation Patterns

Patterns for effectively coordinating multi-agent task execution.

## Core Principle

**Fresh context per task + review between tasks = high quality, fast iteration.**

## Execution Patterns

### Sequential (Tightly Coupled Tasks)
When tasks depend on each other:
1. Dispatch one task at a time
2. Review output after each task
3. Only proceed when current task passes review
4. Fix issues before moving to next task

### Parallel (Independent Tasks)
When tasks are independent:
1. Group tasks by domain (different files, subsystems)
2. Dispatch one agent per domain
3. Review all outputs together after completion
4. Check for conflicts between parallel changes

## Task Dispatch Best Practices

### Good Task Prompts
- **Focused** — One clear problem domain
- **Self-contained** — All context needed is included
- **Specific about output** — What should the agent return?
- **Constrained** — "Don't change files outside of X"

### Bad Task Prompts
- ❌ Too broad: "Fix all the tests"
- ❌ No context: "Fix the race condition"
- ❌ No constraints: Agent might refactor everything
- ❌ Vague output: "Fix it"

## Review Checkpoints

After each task or batch:
1. **Read summary** — Understand what changed
2. **Check for conflicts** — Did agents edit the same code?
3. **Run verification** — Tests, lint, build
4. **Spot check** — Review critical changes manually
