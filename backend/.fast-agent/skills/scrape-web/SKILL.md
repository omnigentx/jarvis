---
name: scrape-web
description: >
  Access and extract content from websites. Use when you need to read URL content,
  scrape data, or fetch information from blocked pages.
  Priority: serpapi → ScraplingServer get → fetch → stealthy_fetch.
---

# Web Content Retrieval

<tool_priority>
Escalation chain — follow this order strictly:
1. serpapi (search/get) → Fast, stable, for most search queries. Always use `gl=vn`, `hl=vi`.
2. ScraplingServer get → When accessing a specific URL directly
3. ScraplingServer fetch → If get returns 403/empty
4. ScraplingServer stealthy_fetch → If fetch is still blocked
</tool_priority>

<rule>
DO NOT use ScraplingServer for search queries. Only use it when you need direct URL access that serpapi cannot provide.
</rule>

### When to Use What

| Scenario | Tool |
|----------|------|
| General information search | `serpapi` → search |
| Google/Bing results | `serpapi` |
| Access specific user-provided URL | ScraplingServer `get` |
| URL returns 403/blocked | ScraplingServer `fetch` |
| Cloudflare/anti-bot protection | ScraplingServer `stealthy_fetch` |
| JS-rendered SPA | ScraplingServer `fetch` + `network_idle: true` |

---

## ScraplingServer — Fallback Details

### Per-Site Recommendations

| Site | Tool |
|------|------|
| LinkedIn, Glassdoor, GitHub, Reddit | `get` |
| Telegram public channel | `get` + URL: `https://t.me/s/CHANNEL` |
| Twitter/X, Twitch, Facebook | `fetch` + `network_idle: true` |
| TikTok profile/page | `stealthy_fetch` + `network_idle: true`, `wait: 4000` |
| TikTok comments | `get` via internal API (see below) |
| Cloudflare-protected | `stealthy_fetch` + `solve_cloudflare: true` |

### Key Parameters

- `extraction_type`: `"text"` · `"markdown"` · `"html"`
- `main_content_only: true` — strips nav/footer noise
- `css_selector` — target specific elements
- `network_idle: true` + `wait: 2000` — for lazy-loaded JS content
- `disable_resources: true` — faster (fetch/stealthy_fetch only)

### Bulk Fetching

```
tool: bulk_get / bulk_fetch
urls: ["https://...", "https://..."]
```

---

## TikTok Comments API

Comments don't render in DOM — call the internal API directly (no login needed):

```
tool: get
url: https://www.tiktok.com/api/comment/list/
params: { aweme_id: "VIDEO_ID", count: "10", cursor: "0", aid: "1988" }
headers: { Referer: "https://www.tiktok.com/@USER/video/VIDEO_ID" }
extraction_type: "html"
main_content_only: false
```

Response JSON fields: `comments[].text`, `comments[].user.nickname`, `comments[].digg_count`, `total`, `has_more`. Paginate by incrementing `cursor` by 10.

To get VIDEO_ID: `stealthy_fetch` the profile page as `html`, extract with regex `/@[\w.]+/video/(\d+)`.
