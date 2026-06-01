---
name: crawling
description: >
  Crawl and download stories from ANY website into the local library —
  site-agnostic, language-agnostic. Use when the user wants to add a new story,
  download from the web, or check crawl progress. Flow: detect structure →
  build recipe → verify 3 chapters → full crawl (worker enumerates).
---

# STORY CRAWLING WORKFLOW (recipe-based, AI-driven)

You analyse page STRUCTURE and emit a small RECIPE. The backend worker enumerates
every chapter from that recipe and downloads them — the chapter list never passes
through you, so this scales to 1000+ chapter stories without burning tokens.

## Decision Tree
```
Add new story → has URL? use it : search_stories(query)
Check progress → get_crawl_status(job_id)
View library  → local_list_stories()
```

## Step 0: GET A URL
- User gave a URL → use it directly (skip search).
- No URL → **serpapi** (Google search, fast) to find the story page. scrapling
  is for fetching a known URL, not searching. (Legacy `web_search_stories` is a
  slow per-site HTTP fallback — avoid unless serpapi is down.)

## Step 1: DETECT STRUCTURE
`detect_story_structure(url)` renders the page (JS-aware) and returns, all
LANGUAGE-AGNOSTIC:
- `link_patterns`: same-domain links grouped by URL shape (digits→#) with counts +
  `within_story_root` flag + `selector_hint`. A high-count within-story pattern =
  the chapter links.
- `content_candidates`: scored by paragraph density (pick narrative, not nav).
- `pagination_hints`, `story_root_guess`.

## Step 2: BUILD RECIPE (JSON)
- **LIST mode** (page lists many chapters):
  `{mode:"list", story_root:"/slug/", overview_url, chapter_url_pattern:"<sample chapter URL>", content_selector, title_selector, next_page_selector?}`
- **CHAIN mode** (single chapter page + a "next" link):
  `{mode:"chain", story_root:"/slug/", first_chapter_url, next_chapter_selector, content_selector, title_selector}`
- `story_root` is REQUIRED in both — the same-story guard that blocks drift into
  recommended/other stories.
- Find `content_selector` on a CHAPTER page (overview pages have no chapter body).
  Re-run detect_story_structure on a sample chapter URL if needed.

## Step 3: VERIFY — MANDATORY (3 chapters)
`verify_chapters(recipe_json)` fetches the first 3 chapters and checks length,
that chapters differ, and URL continuity.
- ok=false → read `issues`, fix the recipe (usually content_selector), re-verify.
- NEVER crawl an unverified recipe.

## Step 4: FULL CRAWL
`crawl_story(recipe_json, max_chapters=..., speed=1.0)` → returns job_id.
Report to the user ending with `[[[CRAWL_STARTED: job_id]]]`.

## Step 5: TRACKING / SELF-HEAL
- Progress on request → `get_crawl_status(job_id)`.
- If a crawl gets stuck, the worker pauses and AUTO-WAKES you with a
  `[CRAWL SELF-HEAL]` message (stuck URL + current recipe). Then:
  detect_story_structure(stuck_url) → diagnose → verify_chapters(fixed) →
  `resume_crawl(job_id, recipe_patch_json=<fix>)`. If it's the real end of the
  story → `resume_crawl(job_id, mark_done=true)`. If the site is blocked / changed
  structurally → tell the user.

<violation>
- Crawling WITHOUT verify_chapters first → VIOLATION.
- A recipe without `story_root` (no same-story guard) → VIOLATION.
- Relying on language-specific words (e.g. "Chương") for detection → use URL/DOM
  structure instead.
</violation>
