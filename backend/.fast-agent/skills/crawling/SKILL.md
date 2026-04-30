---
name: crawling
description: >
  Crawl and download stories from the internet to local library. Use when user wants to add
  new stories, download from web, or check crawl progress. Flow: analyze page → test → full crawl.
---

# STORY CRAWLING WORKFLOW

## Decision Tree

```
What does the user want?
├── Add new story
│   ├── Has link? → Step 0
│   └── No link? → Step 1
├── Check progress → get_crawl_status(job_id)
└── View downloaded stories → local_list_stories()
```

<url_handling>
When user provides a URL:
- If it's an index/overview page → DO NOT use it for analysis
- ACTION: call `get_story_chapters(overview_url)` → get Chapter 1 URL + total_chapters
- Only use the Chapter 1 URL for structure analysis
</url_handling>

## Step 1: FIND CHAPTER 1 URL
- `search_stories(query)` → find story on web

## Step 2: ANALYZE PAGE STRUCTURE (CRITICAL)
- `get_story_page_structure(url)` → find CSS selectors
- Choose selector with highest score (usually contains 'content', 'chapter', long text)
- If "TOP NEXT LINK CANDIDATES" found → `add_story_provider` to teach the system

<verify_step>
Step 3: VERIFICATION — THIS STEP IS MANDATORY
- `test_crawl_chapter(url, content_selector=...)` → check content quality
- You MUST verify before proceeding to full crawl
</verify_step>

<content_validation>
❌ GARBAGE content — choose a different selector:
- Contains "Advertisement", "Sorry", "Posted at...", "Please..."
- Content too short (< 200 characters)
- Repeats story title/author multiple times

✅ VALID content:
- Starts with actual story content ("Chapter 1...", "He opened his eyes...")
- Reasonable length (> 500 characters)

ACTION when garbage found: Drop selector → pick another → test again. Repeat until valid.
</content_validation>

## Step 4: FULL CRAWL
```
crawl_story(url, content_selector="#...", title_selector="h1", speed=1.0, max_chapters=total_chapters)
```
- Returns `job_id`
- Report to user: "Started crawling..." with tag `[[[CRAWL_STARTED: job_id]]]`

## Step 5: TRACKING
- User asks → `get_crawl_status(job_id)`

<violation>
- Starting full crawl WITHOUT running test_crawl_chapter first → VIOLATION
- Using an index/overview page URL for analysis → VIOLATION
</violation>
