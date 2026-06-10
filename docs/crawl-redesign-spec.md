# Crawl Redesign Spec — site-agnostic, AI-driven

> Status: **DRAFT for review** (no code yet). Goal: crawl stories from **any site**,
> language-independent / no per-site hardcoded selectors. For Jarvis OSS.

## 1. Why the old design is not universal (verified with real data)

| # | Problem | Evidence (actually run, not guessed) |
|---|--------|-----|
| 1 | `requests.get` does not render JS | 10/10 fetch sites use `requests`. `truyencom.com` overview is JS-rendered → same-slug scan = **0 links**. `StealthyFetcher` import OK in-process. |
| 2 | Overview resolution via fragile `chuong/quyen` regex | `story_server.py:465` `/(chuong\|chapter\|hoi\|c)[-_]?\d+.*$` does NOT match `/quyen-1-chuong-1/`. Real run: chapter-URL → **6 junk links (sidebar)**, overview-URL → **128 correct**. |
| 3 | Chapter list filtered by Vietnamese-text regex + hardcoded selectors | `:494` container `.list-chapter,#list-chapter,...` → fallback `container=soup` (whole page). `:509` filter `(chương\|chapter\|hồi)\s+\d+` ("chương"/"hồi" = chapter) → swallows "recommended stories" links. Not multilingual. |
| 4 | CHAIN MODE drifts into another story | `:1378-1391` follows the `next` link WITHOUT validating same domain/slug; `:1234,1357` all chapters saved into 1 shared folder with sequential numbering → "a pile of chapters from another story". |
| 5 | Provider matched by substring | `:132` `p['domain'] in url`; 1 selector/domain for all stories. |

**Proven that same-slug is NOT universal:** truyenfull overview → 53 links/page (good); truyencom → 0 (JS). ⇒ static rules cannot be patched on.

## 2. Principle (agreed with user)

> Must **obtain the rendered page structure** → **AI analyzes** to find chapter-list + next-page,
> no hard rules. Verify the **first 3 chapters** (not 1), longer previews, and a **continuity check**:
> the end of chapter X must flow into the start of chapter X+1.

## 3. Architecture: AI produces a RECIPE (small), CODE enumerates the LIST (no tokens)

> **Token principle (stated by user):** a story with 1000+ chapters ⇒ 1000 URLs. The LLM **MUST NOT**
> see those 1000 URLs. The LLM only analyzes the **structure of 1 page** → outputs a small
> **recipe** (selectors + url-pattern + pagination rule). The **worker (code, no-LLM)** uses the recipe
> to enumerate all N chapters. Token cost is nearly **fixed**, does not scale with chapter count.
>
> ⚠️ Fix from spec v1: do NOT pass `chapter_urls=[...1000 urls]` through agent context
> (token explosion). Current code already gets this part right — the worker enumerates by itself;
> we keep that idea and only replace the static detection with an AI-generated recipe.

```
CrawlStoriesAgent (LLM)                         _crawl_worker (code, no-LLM)
──────────────────────                          ────────────────────────────
1. render(overview_url) → DOM structure   ┐
   of 1 PAGE (a few KB, not the list)     │ DETECT      6. enumerate full list using recipe
2. LLM reads structure → outputs small    │ (once,         (pagination, same-story filter)
   RECIPE:                                │  few tokens)   → LLM never sees 1000 URLs → 0 tokens
   { chapter_link_selector,               │             7. fetch each chapter's content per recipe
     next_page_selector,                  │             8. heuristic continuity (all chapters):
     same_story_pattern,                  │                sequential URLs + content dedup
     content_selector }                   │             9. save into folder story_title
3. verify first 3 chapters (LLM reads 3 excerpts) ─┘
4. continuity on 3 chapters (lightweight LLM judge)
5. crawl_story(recipe, overview_url, story_title, max_chapters)
   → pass the RECIPE (small), NOT the list
```

**LLM only touches:** the structure of 1 page + 3 chapter excerpts for verification. Regardless of 50 or 5000 chapters.

**Why the split:** `_crawl_worker` runs inside the FastAPI process, **no LLM available**. The recipe must
be produced in the agent BEFORE calling `crawl_story`; the worker only executes the recipe (deterministic).

## 3bis. Recipe must support 2 MODES (verified on 2 real sites)

| Site | Mode | Evidence (real run, JS rendered) |
|------|------|-----------------------------------|
| truyenfull.today | **LIST** | overview embeds 53 same-slug links/page (×3 pages=128) |
| truyencom.com | **CHAIN** | overview has NO chapter links of its own story (29 "chapter" links are all OTHER stories in the sidebar); the chapter list is obtained by following the next-link from chapter 1 |

⇒ Recipe gets a field `mode: "list" | "chain"`. AI auto-detects during DETECT:
- `list`: `{mode:list, chapter_link_selector, next_page_selector, same_story_pattern}` — worker enumerates from overview + pagination.
- `chain`: `{mode:chain, first_chapter_url, next_chapter_selector, same_story_pattern}` — worker follows next from chapter 1.
- **`same_story_pattern` is MANDATORY in both modes** (e.g. path-prefix `/dao-gioi-thien-ha-convert/` or a slug regex). Worker drops every non-matching link → blocks the 29-other-story-links in code, no LLM needed to review each link. This is the core anti-drift gate.

## 4. Concrete changes

### 4.1 Fetch layer — render JS
- Add helper `render_page(url)` using `scrapling.fetchers.StealthyFetcher` (in-process, import verified).
  Returns rendered HTML. Replace `requests.get` in the **detect** steps (page-structure, chapter-list, verify).
- Worker content fetch: render only if the site needs JS for chapter content (chapters are usually static HTML — measure in practice).

### 4.2 DETECT tools (called by the agent)
- `get_story_page_structure(url)` (changed): render → return **DOM signals** for the LLM to decide on its own, no language-regex self-scoring. Returns: candidate container list (tag, #id, .class, p-count, text-len, link-count, sample link-href-pattern) + next-page candidates. LLM reads → chooses.
- `get_chapter_list(overview_url, list_selector, link_pattern)` (new/changed): render each pagination page, take links inside `list_selector` matching `link_pattern` (provided by the LLM, structural — e.g. "href contains story-slug"), validate same domain+slug. Returns the full URL list.

### 4.3 Stronger VERIFY (per user's intent)
- `verify_chapters(urls[0:3], content_selector, title_selector)`:
  - Fetch the **first 3 chapters**, with a **longer** preview per chapter (e.g. 800–1500 chars, not 200).
  - **Continuity check**: verify content flow from chapter X → X+1. Approach (needs real testing, not final):
    - cheap: chapters must differ (no duplicates), titles increase numerically; OR
    - strong: LLM judge "do the end of X and the start of X+1 flow together / belong to the same story".
  - Returns verdict + reasons so the agent can decide to crawl or switch selectors.

### 4.4 EXECUTE — crawl_story takes a RECIPE (not a list)
- `crawl_story(overview_url, recipe: dict, story_title, max_chapters, speed)`.
  `recipe = {chapter_link_selector, next_page_selector, same_story_pattern, content_selector, title_selector}`.
  Drop auto-detected CHAIN mode. Worker **enumerates the list from the recipe** (pagination + same-story filter)
  → 1000 URLs generated in code, **never through the LLM**.
- `CrawlJob.params` (Text/JSON, already exists) holds the **small recipe** (a few hundred bytes), NOT 1000 URLs.
- Folder = `story_title` provided by the agent (validated), not derived from per-page scraped titles (avoids rename drift).
- `same_story_pattern` (recipe) is the anti-drift gate: worker accepts only links matching the pattern (e.g. same story-slug path-prefix) → filters out sidebar/other stories **in code**, no LLM review per link.

### 4.5 Skill / agent prompt
- `crawling/SKILL.md` + agent: rewrite the workflow as DETECT→VERIFY(3 chapters+continuity)→EXECUTE.
  Remove wording that assumes Vietnamese selectors. Emphasize: always render, always validate same story.

## 4bis. SELF-HEAL: worker hits an error → LLM fixes it (event-driven, NO polling)

> Core requirement (user): the LLM must **verify + fix issues when they occur**, never get stuck.
> Mechanism: **event-driven** — the worker pushes an event to wake the LLM, no polling.

**Architectural facts (verified, file:line):**
- The agent CANNOT be woken **mid-turn**. The inbox only injects between tool-loops of the running turn
  (`inbox_watcher_hook.py:54-105`).
- BUT an **idle** agent CAN be woken via `services/inject_resume.py:resume_with_inject()`
  (the same code path as the dashboard's "Send message to agent" — tested, used in production).
- The crawl worker runs in the background inside the FastAPI process; the crawl agent is by then **idle**
  (it already replied to the user "crawl started"). ⇒ exactly the condition for `resume_with_inject` to wake it.

**Self-heal loop:**
```
Worker detects an anomaly (selector fails N consecutive chapters / empty content /
  drift to a different slug / pagination ends early vs. total)
  │
  ├─ status='needs_attention', SAVE checkpoint (in-progress chapter, current recipe, failing sample HTML)
  │
  └─ PUSH event: resume_with_inject(CrawlStoriesAgent, inject=
        "Crawl job X stuck at chapter 47: <symptom> + <rendered sample HTML>.
         Current recipe: {...}. Diagnose and fix the recipe, or confirm the story has ended.")
        ↓ (event-driven, NO polling)
     LLM wakes up → re-renders the failing page → diagnoses → one of:
        (a) fix the recipe (new selector/pattern) → call resume_crawl(job_id, new_recipe)
        (b) confirm "the story really ended" (47 = last chapter) → mark completed
        (c) report to the user if impossible (site fully restructured / blocked)
        ↓
     Worker resumes from the checkpoint with the new recipe (does NOT re-crawl from the start)
```

**The LLM only wakes on errors** → low token cost. Without errors the worker runs silently to completion.

**Needs adding:**
- `CrawlJob.status` gains a `needs_attention` value; checkpoint column/JSON (in-progress chapter + recipe + sample).
- Worker: anomaly detector (thresholds: K consecutive empty chapters, slug drift, total mismatch).
- New tool `resume_crawl(job_id, recipe_patch)` for the LLM to call after fixing.

**NOTIFY UI (user requirement) — the user MUST know when there is a problem, no silent self-healing:**
Every milestone of the self-heal loop pushes an event to the UI (using existing channels, NO polling):
- Worker detects an anomaly → update `CrawlJob.status='needs_attention'` + a specific message
  ("Stuck at chapter 47: content selector does not match"). The crawl banner (`useCrawlStatus`, already wired)
  polling status switches to a yellow warning state "⚠ Crawl hit a problem — Jarvis is handling it…".
- Push an event through `activity_stream` (SSE → browser, already exists) to show immediately without waiting for a poll tick:
  `{type:'crawl_anomaly', job_id, message}`.
- When the LLM finishes fixing → status back to `running` + message "Fixed, resuming from chapter 47" → banner green again.
- If the LLM gives up (site fully changed / blocked) → status `failed` + a clear reason message for the user.
- Each milestone should also appear in chat (a short assistant message) when the LLM wakes to handle it, so the user
  has context: "The Thiên Ảnh crawl hit a problem at ch.47, I'm trying a different selector…".
- Frontend `useCrawlStatus`: add handling for the `needs_attention` status (warning color + message) and
  listen for the `crawl_anomaly` event for instant updates.

## 4ter. CLEAN UP DEAD CODE + UPDATE SKILL (user requirement)

After the redesign, these tools become **orphaned / duplicated** — delete, don't maintain:
- `test_crawl_chapter` is **defined twice** (`story_server.py:572` AND `:933`) → the later
  `@mcp.tool()` overrides the earlier one ⇒ **L572 is certainly dead code**. Delete one.
- `crawl_story` (`:1012`) vs `crawl_story_full` (`:1426`) → check role overlap, merge into 1.
- `analyze_story_pattern`(`:879`)/`_analyze_story_pattern_impl` + `get_story_page_structure`(`:811`):
  the redesign merges these into 1 detect tool "render → structure for the LLM". Drop the Vietnamese-regex
  scoring (`_find_best_next_selector` text-based scoring) if the LLM picks from the structure itself.
- `add_story_provider` + `StoryConfigManager` substring-match: keep as a **recipe cache** (optimization)
  or drop — decide per phase. If kept: fix matching to exact host, store the recipe instead of loose selectors.
- `find_story_chapter`(`:1631`): belongs to AudioReaderAgent (story playback), a DIFFERENT flow from crawl — **keep**.

**Rule:** before deleting each tool → `grep` callers (agent whitelist in `agent.py`, skill,
route, test). Delete only at 0 real callers. Update the `agent.py` tools whitelist + `crawling/SKILL.md`
to match the new tools (DETECT→VERIFY 3 chapters+continuity→EXECUTE→SELF-HEAL). Run tests after each deletion.

## 5. UNCERTAIN points — must verify while coding (no guessing)
- [ ] `StealthyFetcher` rendering: speed for 1000+ chapter stories (many pagination pages)? Need cache/parallelism?
- [ ] Worker content fetch: which sites need JS rendering for **chapter content** (not just the list)? Measure in practice.
- [ ] Continuity check: heuristic (overlap/dedup/title-seq) or LLM judge? Token cost vs. accuracy.
- [ ] Provider cache: keep `add_story_provider` to skip re-detection next time? (optimization, not required)
- [ ] `crawl_story` signature change → which old callers break? grep callers + update.

## 6. Test plan (multi-site, not just 1 page)
- truyenfull.today (static list, pagination, URL `/quyen-N-chuong-M/`)
- truyencom.com (JS list — must render to see it)
- ≥1 English-language site (proves multilingual support)
- Per site: detect → verify 3 chapters → crawl 5 chapters → assert: correct chapter count, same story (slug), continuous content, no other stories mixed in.

## 7. Blast radius
`tools/story_server.py` (fetch layer, detect tools, crawl_story signature, worker), `core/database.py`
(params shape — already sufficient), `.fast-agent/skills/crawling/SKILL.md`, `agent.py` (CrawlStoriesAgent prompt),
callers of `crawl_story`. **Large** — should be done in phases, testing each phase.
