# MCP Server Catalog

## Escalation Chain (Web Access)

```
serpapi → scrapling-server → chrome-devtools
 (fast)    (direct URL)      (last resort)
```

## Server Reference

| Server | Purpose | When to use |
|--------|---------|-------------|
| serpapi | Google/Bing search | Default for all search queries. Always `gl=vn`, `hl=vi` |
| scrapling-server | Direct URL access, content extraction, anti-bot bypass | When serpapi insufficient or need specific URL |
| chrome-devtools | Advanced browser debugging, CDP | Only when scrapling-server fails |
| time-service | Current time/date | When task is time-sensitive |
| gmail | Email send/read | Personal agent tasks only |
| calendar | Google Calendar | Personal agent tasks only |
| iot-control | Smart home devices | IoT agent only |
| story-server | Story crawling/search | Story agents only |
| library-server | Local story library | Audio reader only |
| media-server | YouTube/music search | Music agent only |
| sequential-thinking | Step-by-step reasoning | Complex reasoning tasks |

## Selection Rules

1. **Minimum viable set** — only assign servers the agent actually needs. Each extra server adds startup time.
2. **Research tasks** → `serpapi` (required) + `scrapling-server` (if URL access needed)
3. **Finance tasks** → `serpapi` + `scrapling-server` + `time-service`
4. **Browser tasks** → `scrapling-server` first (stealthy_fetch), `chrome-devtools` only if needed
5. **Never combine all web servers** — pick the minimum needed for the task
