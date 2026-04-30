# Skill Examples

## Example 1: Simple Research Skill

```yaml
---
name: research
description: >
  Searches and synthesizes information from the internet. Use when the user asks about
  news, events, or needs deep research synthesis.
  Priority: serpapi → ScraplingServer → chrome-devtools.
---
```

```markdown
# Research & Search

## Priority Order

1. serpapi → Fast, stable. Always use `gl=vn`, `hl=vi`
2. ScraplingServer get → Access specific URL from serpapi results
3. ScraplingServer stealthy_fetch → Bypass anti-bot
4. chrome-devtools → LAST RESORT (very slow)

## Rules
- Always cite sources when possible
- Synthesize from multiple sources if serpapi returns rich results
- **NEVER** use ScraplingServer for search queries — only for direct URL access
```

**Why it works:** ~35 lines. Assumes Claude knows what search/scraping is. Provides clear escalation chain. No verbose explanations.

---

## Example 2: Skill with References (Delegation Strategy)

```yaml
---
name: delegation-strategy
description: >
  Guides task delegation to specialized agents and spawn tools. Determines when to
  self-handle vs delegate, selects appropriate MCP servers and skills for spawned agents.
  Use when receiving research tasks, web access needs, or complex multi-step requests.
---
```

```markdown
# Delegation Strategy

## Core Rule
**Do not self-handle tasks requiring web access or research.** Delegate first.

## Decision Flow
1. Existing agent matches? → Use `agent__<Name>` tool
2. No match, short task? → `spawn_and_run_isolated`
3. No match, long task? → `spawn_and_run_background`

## Choosing Servers and Skills
See [SERVERS.md](SERVERS.md) for MCP server catalog.
See [SKILLS.md](SKILLS.md) for skill catalog.
```

**Why it works:** Core decision logic in SKILL.md (~50 lines). Detailed catalogs in separate files — only loaded when agent needs to choose servers/skills. Progressive disclosure in action.

---

## Example 3: Bad Skill (Anti-pattern)

```yaml
---
name: helper
description: Helps with things
---
```

```markdown
# Helper Skill

PDF (Portable Document Format) files are a common file format that contains
text, images, and other content. PDFs were invented by Adobe in the 1990s and
have since become the standard format for document exchange...

You can use pypdf, or pdfplumber, or PyMuPDF, or pdf2image, or camelot,
or tabula-py to process PDFs. Each has its own strengths and weaknesses...
```

**Problems:**
- ❌ Vague name (`helper`)
- ❌ Vague description ("Helps with things")
- ❌ Explains what PDFs are (Claude knows this)
- ❌ Offers 6 options without a default
- ❌ No trigger context in description
