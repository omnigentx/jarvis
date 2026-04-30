---
name: software-architecture
description: >
  Architecture design patterns, ADR templates, and quality rules.
  Use when SA needs to design systems, review code architecture, or write ADRs.
---

# Software Architecture

## Core Principles

### Clean Architecture & DDD
- Domain-driven design with ubiquitous language
- Separate domain entities from infrastructure
- Keep business logic independent of frameworks

### SOLID
- **Single Responsibility** — one reason to change per module
- **Open/Closed** — extend, don't modify
- **Liskov Substitution** — subtypes must be substitutable
- **Interface Segregation** — specific interfaces over general
- **Dependency Inversion** — depend on abstractions

## Code Quality Rules

- **Early returns** over nested conditions (max 3 levels)
- **Functions** under 50 lines
- **Files** under 200 lines — split if longer
- **Library-first** — search for existing solutions first
- **Rule of Three** — abstract only when pattern proven 3+ times

## Anti-Patterns

- ❌ NIH syndrome — use libraries for auth/state/validation
- ❌ Premature optimization — measure first
- ❌ YAGNI — don't build for imaginary requirements
- ❌ God objects — no files with 50+ unrelated functions
- ❌ Generic names: `utils`, `helpers`, `common`

## ADR Template

```markdown
# ADR-[N]: [Decision Title]
## Status: Proposed | Accepted | Deprecated
## Context: [What problem are we solving?]
## Decision: [What we chose and why]
## Consequences: [Trade-offs and impacts]
```

## References

| Topic | File |
|-------|------|
| System design process | [SYSTEM_DESIGN.md](references/SYSTEM_DESIGN.md) |
| Meeting protocol | [MEETING_PROTOCOL.md](references/MEETING_PROTOCOL.md) |
