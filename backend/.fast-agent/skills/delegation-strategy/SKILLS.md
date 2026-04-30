# Skill Catalog

## Available Skills for Spawn

| Skill | Purpose | Typical pairing |
|-------|---------|-----------------|
| research | Web search, news synthesis, source citation | `serpapi` |
| scrape-web | Direct URL access, anti-bot escalation chain | `scrapling-server` |
| finance | Market data, stock/gold/crypto analysis | `serpapi` + `time-service` |
| crawling | Story crawling workflows | `story-server` + `scrapling-server` |
| personal-assistant | Email, calendar, reminders | `gmail` + `calendar` |
| iot-control | Smart home device control | `iot-control` |
| music-playback | Music search and playback | `media-server` |
| audio-reading | Story audio playback | `story-server` + `library-server` |
| user-context | User preferences and habits | (any agent — recommended) |

## Pairing Rules

1. **Always include `user-context`** for agents that interact with the user
2. **Research agents** → `research` + `scrape-web` (covers search + URL access)
3. **Match skills to servers** — a skill without its server is useless
4. **Use comma-separated format**: `skills: "research, scrape-web, user-context"`
