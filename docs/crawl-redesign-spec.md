# Crawl Redesign Spec — site-agnostic, AI-driven

> Status: **DRAFT for review** (chưa code). Mục tiêu: crawl truyện từ **trang bất kỳ**,
> không phụ thuộc ngôn ngữ / không hardcode selector từng site. Cho Jarvis OSS.

## 1. Vì sao thiết kế cũ không phổ quát (đã verify bằng dữ liệu thật)

| # | Vấn đề | Bằng chứng (chạy thật, không đoán) |
|---|--------|-----|
| 1 | `requests.get` không render JS | 10/10 chỗ fetch dùng `requests`. `truyencom.com` overview render bằng JS → same-slug scan = **0 link**. `StealthyFetcher` import OK in-process. |
| 2 | Resolve overview bằng regex `chuong/quyen` mong manh | `story_server.py:465` `/(chuong\|chapter\|hoi\|c)[-_]?\d+.*$` KHÔNG khớp `/quyen-1-chuong-1/`. Chạy thật: chương-URL → **6 link rác (sidebar)**, overview-URL → **128 đúng**. |
| 3 | Chapter list lọc bằng regex text tiếng Việt + selector hardcode | `:494` container `.list-chapter,#list-chapter,...` → fallback `container=soup` (cả trang). `:509` lọc `(chương\|chapter\|hồi)\s+\d+` → nuốt link "truyện đề xuất". Không đa ngôn ngữ. |
| 4 | CHAIN MODE drift sang truyện khác | `:1378-1391` follow `next` link KHÔNG validate cùng domain/slug; `:1234,1357` mọi chương lưu chung 1 folder, số thứ tự nối tiếp → "đống chapter của truyện khác". |
| 5 | Provider match substring | `:132` `p['domain'] in url`; 1 selector/domain cho mọi truyện. |

**Đã chứng minh same-slug KHÔNG phổ quát:** truyenfull overview → 53 link/trang (tốt); truyencom → 0 (JS). ⇒ không thể đắp rule tĩnh.

## 2. Nguyên lý (chốt với user)

> Phải **lấy được cấu trúc trang đã render** → **AI phân tích** tìm chapter-list + next-page,
> không rule cứng. Verify **3 chương đầu** (không phải 1), preview dài hơn, và **continuity check**:
> cuối chương X phải nối mạch đầu chương X+1.

## 3. Kiến trúc: AI ra RECIPE (nhỏ), CODE enumerate LIST (không token)

> **Nguyên tắc token (user nêu):** truyện 1000+ chương ⇒ 1000 URL. LLM **KHÔNG ĐƯỢC**
> nhìn thấy 1000 URL đó. LLM chỉ phân tích **cấu trúc 1 trang** → output một **recipe**
> nhỏ (selectors + url-pattern + pagination rule). **Worker (code, no-LLM)** dùng recipe
> để enumerate đủ N chương. Token gần như **cố định**, không scale theo số chương.
>
> ⚠️ Sửa lỗi spec v1: KHÔNG truyền `chapter_urls=[...1000 url]` qua context agent
> (sẽ nổ token). Code hiện tại đã đúng phần này — worker tự enumerate; ta giữ nguyên ý đó,
> chỉ thay phần detect tĩnh bằng recipe do AI sinh.

```
CrawlStoriesAgent (LLM)                         _crawl_worker (code, no-LLM)
──────────────────────                          ────────────────────────────
1. render(overview_url) → DOM structure   ┐
   của 1 TRANG (vài KB, không phải list)  │ DETECT      6. enumerate full list bằng recipe
2. LLM đọc structure → output RECIPE nhỏ: │ (1 lần,        (pagination, same-story filter)
   { chapter_link_selector,               │  token nhỏ)    → LLM KHÔNG thấy 1000 URL → 0 token
     next_page_selector,                  │             7. fetch content từng chương theo recipe
     same_story_pattern,                  │             8. heuristic continuity (toàn bộ):
     content_selector }                   │                URL tuần tự + dedup nội dung
3. verify 3 chương đầu (LLM đọc 3 đoạn) ──┘             9. lưu vào folder story_title
4. continuity 3 chương (LLM judge nhẹ)
5. crawl_story(recipe, overview_url, story_title, max_chapters)
   → truyền RECIPE (nhỏ), KHÔNG truyền list
```

**LLM chỉ chạm:** structure 1 trang + 3 đoạn chương verify. Bất kể 50 hay 5000 chương.

**Lý do tách:** `_crawl_worker` chạy trong FastAPI process, **không có LLM**. Recipe phải
sinh xong ở agent TRƯỚC khi gọi `crawl_story`; worker chỉ thực thi recipe (deterministic).

## 3bis. Recipe phải hỗ trợ 2 MODE (verify bằng 2 site thật)

| Site | Mode | Bằng chứng (chạy thật, render JS) |
|------|------|-----------------------------------|
| truyenfull.today | **LIST** | overview nhúng 53 link/trang cùng slug (×3 trang=128) |
| truyencom.com | **CHAIN** | overview KHÔNG có link chương của chính nó (29 link "chương" đều là truyện KHÁC ở sidebar); list chương lấy bằng follow next-link từ chương 1 |

⇒ Recipe có field `mode: "list" | "chain"`. AI tự nhận diện khi detect:
- `list`: `{mode:list, chapter_link_selector, next_page_selector, same_story_pattern}` — worker enumerate từ overview + pagination.
- `chain`: `{mode:chain, first_chapter_url, next_chapter_selector, same_story_pattern}` — worker follow next từ chương 1.
- **`same_story_pattern` BẮT BUỘC ở cả 2 mode** (vd path-prefix `/dao-gioi-thien-ha-convert/` hoặc regex slug). Worker loại mọi link không khớp → chặn 29-link-truyện-khác bằng code, không cần LLM duyệt từng link. Đây là chốt chặn drift cốt lõi.

## 4. Thay đổi cụ thể

### 4.1 Fetch layer — render JS
- Thêm helper `render_page(url)` dùng `scrapling.fetchers.StealthyFetcher` (in-process, đã verify import).
  Trả HTML đã render. Thay `requests.get` ở các bước **detect** (page-structure, chapter-list, verify).
- Worker fetch content: render nếu site cần JS cho nội dung chương (thường chương là HTML tĩnh — đo thực tế).

### 4.2 DETECT tools (agent gọi)
- `get_story_page_structure(url)` (đổi): render → trả **DOM signals** cho LLM tự quyết, không tự chấm điểm bằng regex ngôn ngữ. Trả: danh sách container ứng viên (tag, #id, .class, p-count, text-len, link-count, link-href-pattern mẫu) + ứng viên next-page. LLM đọc → chọn.
- `get_chapter_list(overview_url, list_selector, link_pattern)` (mới/đổi): render từng trang pagination, lấy link trong `list_selector` khớp `link_pattern` (LLM cung cấp, structural — vd "href chứa story-slug"), validate cùng domain+slug. Trả full list URL.

### 4.3 VERIFY mạnh hơn (theo ý user)
- `verify_chapters(urls[0:3], content_selector, title_selector)`:
  - Fetch **3 chương đầu**, preview mỗi chương **dài hơn** (vd 800–1500 ký tự, không phải 200).
  - **Continuity check**: kiểm tra mạch nội dung chương X → X+1. Cách (cần thử thật, chưa chốt):
    - rẻ: chương phải khác nhau (không trùng), title tăng dần theo số; HOẶC
    - mạnh: LLM judge "đoạn cuối X và đầu X+1 có liền mạch / cùng truyện không".
  - Trả verdict + lý do để agent quyết định crawl hay đổi selector.

### 4.4 EXECUTE — crawl_story nhận RECIPE (không phải list)
- `crawl_story(overview_url, recipe: dict, story_title, max_chapters, speed)`.
  `recipe = {chapter_link_selector, next_page_selector, same_story_pattern, content_selector, title_selector}`.
  Bỏ CHAIN-mode-tự-detect. Worker **enumerate list bằng recipe** (pagination + same-story filter)
  → 1000 URL sinh trong code, **không qua LLM**.
- `CrawlJob.params` (Text/JSON, đã có) chứa **recipe nhỏ** (vài trăm byte), KHÔNG chứa 1000 URL.
- Folder = `story_title` do agent cung cấp (đã validate), không suy từ title scrape từng trang (tránh drift đổi tên).
- `same_story_pattern` (recipe) là chốt chặn drift: worker chỉ nhận link khớp pattern (vd cùng path-prefix story-slug) → loại sidebar/truyện khác **bằng code**, không cần LLM duyệt từng link.

### 4.5 Skill / agent prompt
- `crawling/SKILL.md` + agent: viết lại workflow theo DETECT→VERIFY(3 chương+continuity)→EXECUTE.
  Bỏ ngôn từ giả định selector tiếng Việt. Nhấn: luôn render, luôn validate cùng story.

## 4bis. SELF-HEAL: worker gặp lỗi → LLM sửa (event-driven, KHÔNG polling)

> Yêu cầu cốt lõi (user): LLM phải **verify + sửa sai khi gặp vấn đề**, không stuck.
> Cơ chế: **event-driven** — worker push event đánh thức LLM, không polling.

**Sự thật kiến trúc (đã verify, file:line):**
- KHÔNG wake được agent **giữa turn**. Inbox chỉ inject giữa các tool-loop của turn đang chạy
  (`inbox_watcher_hook.py:54-105`).
- NHƯNG wake được agent **đang idle** bằng `services/inject_resume.py:resume_with_inject()`
  (chính là code path "Send message to agent" của dashboard — đã có test, dùng thật).
- Worker crawl chạy nền trong FastAPI process; agent crawl lúc đó **đã idle** (đã trả lời user
  "đã bắt đầu crawl"). ⇒ đúng điều kiện để `resume_with_inject` đánh thức.

**Vòng self-heal:**
```
Worker phát hiện anomaly (selector fail N chương liên tiếp / nội dung rỗng /
  drift khác slug / pagination đứt sớm so với total)
  │
  ├─ status='needs_attention', LƯU checkpoint (chương đang dở, recipe hiện tại, sample HTML lỗi)
  │
  └─ PUSH event: resume_with_inject(CrawlStoriesAgent, inject=
        "Crawl job X kẹt ở chương 47: <triệu chứng> + <sample HTML đã render>.
         Recipe hiện tại: {...}. Chẩn đoán và sửa recipe, hoặc xác nhận đã hết truyện.")
        ↓ (event-driven, KHÔNG poll)
     LLM thức dậy → render lại trang lỗi → chẩn đoán → 1 trong:
        (a) sửa recipe (selector/pattern mới) → gọi resume_crawl(job_id, new_recipe)
        (b) xác nhận "hết truyện thật" (47 = chương cuối) → mark completed
        (c) báo user nếu bất khả (site đổi cấu trúc hẳn / bị chặn)
        ↓
     Worker resume từ checkpoint với recipe mới (KHÔNG crawl lại từ đầu)
```

**LLM chỉ thức khi có lỗi** → token thấp. Không lỗi thì worker chạy hết im lặng.

**Cần thêm:**
- `CrawlJob.status` thêm giá trị `needs_attention`; cột/JSON checkpoint (chương dở + recipe + sample).
- Worker: bộ phát hiện anomaly (ngưỡng: K chương rỗng liên tiếp, drift slug, total mismatch).
- Tool mới `resume_crawl(job_id, recipe_patch)` cho LLM gọi sau khi sửa.

**NOTIFY UI (user yêu cầu) — user PHẢI biết khi có vấn đề, không self-heal ngầm:**
Mỗi mốc của vòng self-heal đẩy event ra UI (dùng kênh đã có, KHÔNG polling):
- Worker phát hiện anomaly → cập nhật `CrawlJob.status='needs_attention'` + message cụ thể
  ("Kẹt ở chương 47: selector nội dung không khớp"). Banner crawl (`useCrawlStatus`, đã wire)
  poll status sẽ đổi sang trạng thái cảnh báo vàng "⚠ Crawl gặp vấn đề — Jarvis đang xử lý…".
- Đẩy event qua `activity_stream` (SSE → browser, đã có) để hiện ngay không chờ poll-tick:
  `{type:'crawl_anomaly', job_id, message}`.
- Khi LLM sửa xong → status về `running` + message "Đã sửa, tiếp tục từ chương 47" → banner xanh lại.
- Nếu LLM bó tay (site đổi hẳn / bị chặn) → status `failed` + message rõ lý do cho user.
- Mọi mốc cũng nên xuất hiện ở chat (assistant message ngắn) khi LLM thức xử lý, để user
  có ngữ cảnh: "Crawl Thiên Ảnh gặp vấn đề ở ch.47, mình đang thử selector khác…".
- Frontend `useCrawlStatus`: thêm xử lý status `needs_attention` (màu cảnh báo + message) và
  lắng nghe event `crawl_anomaly` để cập nhật tức thì.

## 4ter. DỌN DEAD CODE + UPDATE SKILL (user yêu cầu)

Sau redesign, các tool sau **thành mồ côi / trùng** — xoá, không maintain:
- `test_crawl_chapter` **định nghĩa 2 lần** (`story_server.py:572` VÀ `:933`) → `@mcp.tool()` sau
  override trước ⇒ **L572 là dead code chắc chắn**. Xoá 1.
- `crawl_story` (`:1012`) vs `crawl_story_full` (`:1426`) → kiểm tra trùng vai trò, gộp còn 1.
- `analyze_story_pattern`(`:879`)/`_analyze_story_pattern_impl` + `get_story_page_structure`(`:811`):
  redesign gộp thành 1 tool detect "render → structure cho LLM". Bỏ scoring regex tiếng Việt
  (`_find_best_next_selector` text-based scoring) nếu LLM tự chọn từ structure.
- `add_story_provider` + `StoryConfigManager` substring-match: giữ làm **cache recipe** (tối ưu)
  hay bỏ — quyết theo phase. Nếu giữ: sửa match đúng host, lưu recipe thay selector rời.
- `find_story_chapter`(`:1631`): của AudioReaderAgent (đọc truyện), KHÁC luồng crawl — **giữ**.

**Quy tắc:** trước khi xoá mỗi tool → `grep` callers (agent whitelist trong `agent.py`, skill,
route, test). Chỉ xoá khi 0 caller thật. Cập nhật `agent.py` tools whitelist + `crawling/SKILL.md`
cho khớp tool mới (DETECT→VERIFY 3 chương+continuity→EXECUTE→SELF-HEAL). Chạy test sau mỗi lần xoá.

## 5. Điểm CHƯA chắc — phải verify khi code (không đoán)
- [ ] `StealthyFetcher` render: tốc độ với truyện 1000+ chương (pagination nhiều trang)? Có cần cache/parallel?
- [ ] Worker fetch content: site nào cần render JS cho **nội dung chương** (không chỉ list)? Đo thực tế.
- [ ] Continuity check: dùng heuristic (overlap/dedup/title-seq) hay LLM judge? Token cost vs độ chính xác.
- [ ] Provider cache: còn giữ `add_story_provider` để lần sau khỏi detect lại không? (tối ưu, không bắt buộc)
- [ ] `crawl_story` đổi signature → ai gọi cũ bị vỡ? grep callers + cập nhật.

## 6. Test plan (đa site, không chỉ 1 trang)
- truyenfull.today (static list, pagination, URL `/quyen-N-chuong-M/`)
- truyencom.com (JS list — phải render mới thấy)
- ≥1 site tiếng Anh (chứng minh đa ngôn ngữ)
- Mỗi site: detect → verify 3 chương → crawl 5 chương → assert: đúng số chương, cùng story (slug), nội dung liền mạch, không lẫn truyện khác.

## 7. Blast radius
`tools/story_server.py` (fetch layer, detect tools, crawl_story signature, worker), `core/database.py`
(params shape — đã đủ), `.fast-agent/skills/crawling/SKILL.md`, `agent.py` (CrawlStoriesAgent prompt),
callers của `crawl_story`. **Lớn** — nên làm theo phase, test từng phase.
