# OSS English-First Backlog

The project is going open-source (global). Per `CLAUDE.md §7`, **all code, comments,
docstrings, log strings, and prompt examples must be English.** Vietnamese is allowed
**only** as (a) `vi+en` code-switching **test data**, (b) site-language crawl markers in
`backend/helpers/crawl_markers.py`, (c) language-detection / tokenizer data, (d) TTS
sample text matching a Vietnamese voice, and (e) the `vi` side of a proper bilingual
`useLang()` / `L('vi','en')` UI string.

Scope of this scan: every tracked file except vendored trees (`.venv`, `node_modules`,
`mcp-atlassian`, `fast-agent`, `.fast-agent`), binaries, lockfiles, and `locales/*.json`
(legitimately bilingual). Re-run the scanner any time:

```bash
# flags every line containing a Vietnamese-specific letter, bucketed by area
python3 scripts/scan_vietnamese.py   # (or the inline script in the audit thread)
```

Totals at scan time (2026-06-24): **54 VI lines in prod source / 156 in test+fixture /
10 in docs**, across 66 files. Most are legitimate (see "KEEP" below); the actionable
items are listed by priority.

---

## P0 — PII scrub (BLOCKER before public; tracked in the security audit)

Real maintainer PII used as illustrative examples. **Must be removed from source, tests,
docs AND git history** before the repo goes public (repo is currently private — history
rewrite is still feasible). Replace with neutral placeholders
(`User` / `likes` / `works at` / `tea` / `AcmeCorp` / `downtown` / `a pet`).

- [ ] **RUNTIME leak → cloud LLM:** `backend/services/memory/knowledge_graph.py:28` —
      `phở, Techcombank, Gia Lâm, con trai, guitar` inside `TRIPLE_PROMPT` (sent to the
      LLM on every triple extraction). Drop the PII tokens.
- [ ] `backend/services/indexing/ladybug_store.py:386` — real name `Nguyễn Văn Phúc`.
- [ ] `backend/services/indexing/ladybug_store.py:437` — `(Người dùng)-[làm việc tại]->(Techcombank)`.
- [ ] `backend/services/memory/candidate_service.py:231` — `(Techcombank→FPT)`.
- [ ] `backend/services/memory/settings.py:63` — `→ Techcombank 0.245`.
- [ ] `backend/services/retrieval/fusion.py:67` — `works at FPT / works at Techcombank`.
- [ ] `backend/services/retrieval/orchestrator.py:115-116` — `FPT / Techcombank`.
- [ ] `backend/services/retrieval/reranker.py:86` — `where do I work → Techcombank`.
- [ ] `frontend/src/components/memory/MemoryGraph.vue:6` — comment `(Người dùng)-[làm việc tại]->(Techcombank)`.
- [ ] `docs/memory-v2-plan.md` — PII in the plan doc.
- [ ] **Tests/fixtures** containing real PII (not just VI): `test_memory_orchestrator.py`,
      `approvals_cron_pending.yaml`, `team_monitor_v2_terminal.yaml`,
      `approvals-mobile-responsive.spec.ts`, `team-monitor-v2.spec.ts`.
- [ ] **Git history:** 7 commits contain `Techcombank` (e.g. `25b8732` = PR #96, already
      on `main`); name in `14dac72`. Decide squash/rewrite before first public push.

---

## P1 — Vietnamese examples that SHIP (docstrings / prompts) → English

These are not bilingual UI; they are English-context code whose *example* happens to be
Vietnamese. Translate the example (keep the surrounding English).

- [ ] `backend/services/memory/knowledge_graph.py:4` — docstring example
      `"người dùng thích ăn phở" → {"s":"Người dùng","p":"thích","o":"phở"}`.
      ⚠️ **Care:** the `TRIPLE_PROMPT` (lines 20-33) intentionally guides the LLM to emit
      Vietnamese predicates for Vietnamese input ("Keep the statement's language"). Don't
      blindly anglicize the few-shot or you degrade VI extraction — make it **bilingual**
      (an English example AND a VI example) and remove only the PII tokens.
- [ ] `backend/services/indexing/ladybug_store.py:436-437` — docstring relation example
      `(Người dùng)-[thích]->(phở)` → `(User)-[likes]->(tea)`.
- [ ] `backend/services/indexing/ladybug_store.py:28` — docstring `'Phở' and 'phở '` →
      `'Tea' and 'tea '`.
- [ ] `backend/core/database.py:761` — comment example `{"s":"Người dùng","p":"thích","o":"phở"}` → English.

## P2 — Vietnamese fragments in code comments → English

Pure-English comments that quote a Vietnamese phrase. Low risk, but "all comments English".

- [ ] `frontend/src/composables/voiceChatBinding.js:18,22` — `"nói chen"`, `"câu trước biến mất" bug`.
- [ ] `backend/routes/chat.py:226` — `("Đang phát ... chương 1")`.
- [ ] `backend/services/crawl_poller.py:230` — `"Trang tiếp"/"Next"` (quoting the marker text).
- [ ] `frontend/src/composables/useVoiceSession.js:679` — `a VN "Xin chào" greeting`.
- [ ] `frontend/src/utils/youtubeTags.js:69` — `("phát nhạc <bài>")`.
- [ ] `backend/services/indexing/fts_index.py:63-64` — VI illustrative query tokens in the
      English docstring (`"2 cộng 2"`, `"thời tiết gì"`).

## P3 — Tests & fixtures (156 VI lines)

Policy: `vi+en` code-switching IS the feature, so Vietnamese **test data** stays. But:
- [ ] Test/fixture **comments** must be English (same rule as prod).
- [ ] Real **PII** in fixtures must be scrubbed (see P0) — keep VI, swap to fake names
      (`Nguyễn Văn A`, a generic company) instead of the maintainer's real details.
- [ ] Where a VI string is an arbitrary label (not exercising code-switching), prefer a
      neutral one so a non-VI contributor can read the test.

(Not enumerated line-by-line here — run the scanner and apply the policy per file.)

---

## KEEP — legitimate Vietnamese, do NOT "fix" (would break behavior)

- `backend/helpers/crawl_markers.py` — site-language crawl markers (`chương/hồi/mở đầu/Trang tiếp/Truyện`). §7 says markers live here.
- `backend/tools/story_server.py:639,736,739,1533,1535` — VI story-site crawl regexes
  (`chương`, `đọc từ đầu`, `tiếp`). Functional crawl data. *(Optional refactor: centralize
  into `crawl_markers.py` per §7 — low priority, not a wording bug.)*
- `backend/services/indexing/fts_index.py:48-50` — Vietnamese stopword list (tokenizer data).
- `backend/services/memory/knowledge_graph.py:64` — `_GENERIC_SUBJECTS` includes
  `"người dùng"` to match Vietnamese user input at runtime.
- `frontend/src/views/settings/SettingsVoice.vue:194` — `vi:` TTS preview sample, must
  match the Vietnamese voice (§7).
- `frontend/src/components/AppLayout.vue:63-78` — already bilingual (`lang.value === 'vi' ? … : …`).
- `frontend/src/components/memory/MemoryGraph.vue:141-175` — already bilingual via `L('vi','en')`.
