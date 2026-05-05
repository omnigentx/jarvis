---
name: research
description: >
  Search and synthesise information from the internet. Use when the user
  asks about news, events, or needs deep multi-source research.
  Tool priority: serpapi → ScraplingServer → chrome-devtools.
---

# RESEARCH AND SEARCH

## This skill vs `scrape-web`

| Situation | Which skill |
|---|---|
| Multi-source synthesis search | **research** (this skill) |
| Visit a specific URL the user provided | `scrape-web` |
| Page is blocked / needs bypass | `scrape-web` |

## Priority order

```
1. serpapi → Fast, reliable. Match the user's locale (e.g. gl=vn, hl=vi for Vietnamese users).
   ↓ Not enough results?
2. ScraplingServer get → Hit specific URLs from the serpapi result list.
   ↓ Got a 403?
3. ScraplingServer fetch / stealthy_fetch → Bypass anti-bot.
   ↓ Need interactive flow (login, click)?
4. chrome-devtools → LAST RESORT (very slow).
```

## Rules
- Always cite the source when possible.
- Synthesise from multiple sources when serpapi returns rich results.
- DO NOT use ScraplingServer for search queries — only for accessing specific URLs.
- DO NOT use chrome-devtools unless interaction (click, login) is unavoidable.
