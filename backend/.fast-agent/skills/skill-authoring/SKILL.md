---
name: skill-authoring
description: >
  Creates and maintains fast-agent skills following best practices. Teaches progressive
  disclosure (metadata → SKILL.md → reference files), concise writing, and proper YAML
  frontmatter. Use when asked to create, improve, or review skills for any agent.
---

# Skill Authoring Guide

## Progressive Disclosure (3 Levels)

```
Level 1: Metadata (always loaded, ~100 tokens)
  └─ name + description in YAML frontmatter

Level 2: SKILL.md body (loaded on trigger)
  └─ Core instructions, decision logic, quick references
  └─ Keep under 500 lines / ~5k tokens

Level 3: Reference files (loaded on-demand)
  └─ Detailed catalogs, examples, API references
  └─ Linked from SKILL.md with relative paths
```

## Creating a Skill — Checklist

1. **Choose a name** — lowercase, hyphens only, gerund preferred (`analyzing-data`, not `data`)
2. **Write description** — 3rd person, include WHAT + WHEN trigger
3. **Write SKILL.md body** — only what Claude doesn't already know
4. **Split if >500 lines** — move details to reference files
5. **Save to** `.fast-agent/skills/<skill-name>/SKILL.md`

## YAML Frontmatter Template

```yaml
---
name: skill-name-here
description: >
  Does X and Y. Use when the user asks about Z
  or when the task involves W.
---
```

Rules:
- `name`: max 64 chars, lowercase + hyphens only
- `description`: max 1024 chars, non-empty, 3rd person
- Description must include **what** the skill does AND **when** to use it

## Body Writing Rules

1. **Assume Claude is smart** — don't explain basic concepts
2. **Provide defaults** — don't offer 5 options, give 1 default + escape hatch
3. **Use tables** for quick-reference mappings
4. **Use code blocks** for exact commands or templates
5. **Link reference files** one level deep only (SKILL.md → file.md, never file.md → file2.md)
6. **Consistent terminology** — pick one term, use it everywhere

## File Structure Patterns

See [STRUCTURE.md](STRUCTURE.md) for directory layout patterns.
See [EXAMPLES.md](EXAMPLES.md) for complete skill examples.

## Anti-patterns

- ❌ Verbose explanations Claude already knows
- ❌ Multiple options without a default
- ❌ Time-sensitive info (use "old patterns" section instead)
- ❌ Nested references (file.md → other.md → actual info)
- ❌ Windows paths (`\`) — always use forward slashes
- ❌ 1st/2nd person in description ("I can help you..." → "Processes X files")
- ❌ Vague names: `helper`, `utils`, `tools`
