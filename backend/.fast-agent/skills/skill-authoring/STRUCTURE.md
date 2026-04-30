# Skill Directory Structures

## Simple Skill (single file)

```
my-skill/
└── SKILL.md          # Everything in one file (<500 lines)
```

Use when: skill is concise and self-contained.

## Standard Skill (with references)

```
my-skill/
├── SKILL.md          # Core logic + links to references
├── REFERENCE.md      # Detailed API/catalog/tables
└── EXAMPLES.md       # Input/output examples
```

Use when: core logic fits in SKILL.md but details would bloat it.

## Domain Skill (organized by topic)

```
bigquery-skill/
├── SKILL.md          # Navigation + quick start
└── reference/
    ├── finance.md    # Finance tables/metrics
    ├── sales.md      # Sales pipeline data
    └── product.md    # Product usage metrics
```

Use when: skill covers multiple domains, and each domain is independent.

## Skill with Scripts

```
pdf-processing/
├── SKILL.md          # Workflow + script usage
├── FORMS.md          # Form-filling guide
└── scripts/
    ├── analyze.py    # Extract form fields
    ├── fill.py       # Apply values
    └── validate.py   # Check output
```

Use when: deterministic operations benefit from pre-made scripts.

## fast-agent Specific Paths

Skills directory: `.fast-agent/skills/`

```
.fast-agent/
├── skills/
│   ├── research/SKILL.md
│   ├── scrape-web/SKILL.md
│   ├── finance/SKILL.md
│   └── your-new-skill/SKILL.md    ← Create here
├── agent_cards/                    ← References skills by name
└── sessions/
```

Skills are referenced in agent cards or code by their folder name (e.g., `research`, `scrape-web`).
Skills reload dynamically — no backend restart needed.
