"""
Crawl Poller — Background poller that runs in FastAPI process (long-lived).

Architecture:
  MCP tool `crawl_story()` inserts a pending CrawlJob into DB.
  CrawlPoller polls DB every 5s, picks up pending jobs, runs crawl in threads.
  
  This decouples crawl execution from MCP subprocess lifecycle.
  MCP subprocess can be killed by FastAgent without affecting crawl progress.
  
  Pattern: "call-now, fetch-later" (SEP-1686 MCP Task Primitive)
"""
import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from helpers.http_safety import get_capped_text
from helpers.path_safety import safe_story_path

import requests
from bs4 import BeautifulSoup

from core.database import get_db_session, CrawlJob, DATA_DIR

logger = logging.getLogger("crawl_poller")

_log_path = os.path.join(DATA_DIR, "crawl_debug.log")


def _dbg(job_id: str, msg: str):
    line = f"[{time.strftime('%H:%M:%S')}][{job_id[:8]}] {msg}"
    logger.info(line)
    try:
        with open(_log_path, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


_CHAPTER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def render_page_or_get(url: str, headers: dict) -> str | None:
    """Fetch a CHAPTER page. Chapter bodies are usually static HTML (verified:
    truyenfull #chapter-c served by plain requests) → try fast ``requests``
    first, fall back to JS render only if the static fetch yields too little.
    Keeps the common case fast while still handling JS-built chapter pages."""
    try:
        resp = requests.get(url, headers=headers or _CHAPTER_HEADERS, timeout=(3, 15))
        if resp.status_code == 200 and len(resp.text) > 2000:
            return resp.text
    except Exception:
        pass
    # Fallback: JS render (handles SPA chapter pages)
    try:
        from tools.story_server import render_page
        return render_page(url)
    except Exception as e:
        logger.warning("[CRAWL_POLLER] render fallback failed for %s: %s", url, e)
        return None


class CrawlPoller:
    """Background poller that picks up pending crawl jobs from DB."""
    
    POLL_INTERVAL = 5  # seconds
    
    def __init__(self):
        self._running = False
        self._active_threads: dict[str, threading.Thread] = {}
        self._loop = None  # main event loop — captured in start(), used by worker
                           # threads to schedule async self-heal turns thread-safely

    async def start(self):
        """Main loop — runs forever within FastAPI lifespan."""
        self._running = True
        # Capture the FastAPI event loop so crawl worker THREADS can hand a
        # self-heal agent turn back to it via run_coroutine_threadsafe.
        self._loop = asyncio.get_running_loop()
        logger.info("[CRAWL_POLLER] Started, polling every %ds", self.POLL_INTERVAL)
        
        # On startup: mark stale 'running' jobs as 'pending' for retry
        self._recover_stale_jobs()
        
        while self._running:
            try:
                self._pick_pending_jobs()
            except Exception as e:
                logger.error("[CRAWL_POLLER] Error in poll loop: %s", e, exc_info=True)
            await asyncio.sleep(self.POLL_INTERVAL)
    
    def stop(self):
        self._running = False
        logger.info("[CRAWL_POLLER] Stopped")
    
    def _recover_stale_jobs(self):
        """On startup, reset 'running' jobs back to 'pending' for retry."""
        try:
            db = get_db_session()
            stale = db.query(CrawlJob).filter(CrawlJob.status == "running").all()
            for job in stale:
                logger.info("[CRAWL_POLLER] Recovering stale job %s → pending", job.job_id[:8])
                job.status = "pending"
            db.commit()
            db.close()
        except Exception as e:
            logger.error("[CRAWL_POLLER] Recovery failed: %s", e)
    
    def _pick_pending_jobs(self):
        """Query DB for pending jobs, start crawl thread for each."""
        db = get_db_session()
        try:
            pending = db.query(CrawlJob).filter(CrawlJob.status == "pending").all()
            for job in pending:
                if job.job_id in self._active_threads:
                    continue  # Already being processed
                
                job.status = "running"
                job.updated_at = datetime.now().timestamp()
                db.commit()
                
                params = json.loads(job.params) if job.params else {}
                logger.info("[CRAWL_POLLER] Picked up job %s → %s", job.job_id[:8], job.start_url)
                
                thread = threading.Thread(
                    target=self._crawl_worker,
                    args=(job.job_id, job.start_url, params),
                    name=f"crawl-{job.job_id[:8]}",
                )
                thread.daemon = True
                thread.start()
                self._active_threads[job.job_id] = thread
        finally:
            db.close()
    
    def _update_job(self, job_id: str, **kwargs):
        """Update CrawlJob in DB."""
        try:
            db = get_db_session()
            job = db.query(CrawlJob).filter(CrawlJob.job_id == job_id).first()
            if job:
                for k, v in kwargs.items():
                    setattr(job, k, v)
                job.updated_at = datetime.now().timestamp()
                db.commit()
            db.close()
        except Exception as e:
            logger.error("[CRAWL_POLLER] DB update failed for %s: %s", job_id[:8], e)
    
    def _get_job_status(self, job_id: str) -> str:
        """Read current job status from DB (for cancel checks)."""
        try:
            db = get_db_session()
            job = db.query(CrawlJob).filter(CrawlJob.job_id == job_id).first()
            status = job.status if job else "not_found"
            db.close()
            return status
        except Exception:
            return "unknown"
    
    def _enumerate_list_mode(self, job_id: str, overview_url: str, recipe: dict) -> list[dict]:
        """LIST mode: render overview (+ pagination) and collect chapter URLs that
        match the recipe's chapter_url_pattern AND live under story_root.

        Language-agnostic: filters by URL structure (path-pattern + story_root
        prefix), NOT by chapter-title words. This is the same-story guard that
        stops sidebar "recommended story" links (different slug) from leaking in.
        """
        from urllib.parse import urlparse
        from tools.story_server import render_page, _url_path_pattern

        story_root = recipe.get("story_root") or "/"
        want_pat = recipe.get("chapter_url_pattern")
        want_pat = _url_path_pattern(want_pat.rstrip("/")) if want_pat else None
        next_sel = recipe.get("next_page_selector")
        base_host = urlparse(overview_url).netloc

        seen: set[str] = set()
        chapters: list[dict] = []
        page_url = overview_url
        visited_pages: set[str] = set()
        pages = 0
        while page_url and pages < 100:
            if page_url in visited_pages:
                break
            visited_pages.add(page_url)
            html = render_page(page_url)
            if not html:
                break
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                full = urljoin(page_url, a["href"])
                pp = urlparse(full)
                if (pp.netloc or base_host) != base_host:
                    continue
                path = pp.path
                # same-story guard: must live under the story root
                if not path.startswith(story_root):
                    continue
                if path.rstrip("/") == story_root.rstrip("/"):
                    continue
                # structural pattern match (digits collapsed) — language-agnostic
                if want_pat and _url_path_pattern(path.rstrip("/")) != want_pat:
                    continue
                if full in seen:
                    continue
                seen.add(full)
                chapters.append({"url": full, "title": a.get_text(strip=True)})
            # ── pagination (structural, language-agnostic) ──
            # 1) recipe-supplied selector, 2) rel=next, 3) discover ALL paginated
            #    list pages by collecting same-story links that look like page
            #    indexes (e.g. /story/trang-2/, ?page=2) and queueing the unseen
            #    ones. (3) survives sites whose "next" link is text-only like
            #    "Trang tiếp"/"Next" — we don't read the words, we match the
            #    page-URL shape under story_root.
            nxt = None
            if next_sel:
                n = soup.select_one(next_sel)
                if n and n.get("href"):
                    nxt = urljoin(page_url, n["href"])
            if not nxt:
                n = soup.select_one("a[rel='next']")
                if n and n.get("href"):
                    nxt = urljoin(page_url, n["href"])
            if not nxt:
                for a in soup.find_all("a", href=True):
                    full = urljoin(page_url, a["href"]).split("#")[0]
                    pp = urlparse(full)
                    if (pp.netloc or base_host) != base_host:
                        continue
                    if not pp.path.startswith(story_root):
                        continue
                    # page-index shape: .../trang-N/ , .../page/N , ?page=N , .../N/
                    if re.search(r"(trang|page)[-/_]?\d+|[?&]page=\d+", full, re.I):
                        if full not in visited_pages:
                            nxt = full
                            break
            page_url = nxt.split("#")[0] if (nxt and nxt.split("#")[0] not in visited_pages) else None
            pages += 1

        # Natural sort by the numbers embedded in each chapter URL (quyen, chuong…)
        def _numkey(c):
            nums = [int(x) for x in re.findall(r"\d+", urlparse(c["url"]).path)]
            return nums or [0]
        chapters.sort(key=_numkey)
        _dbg(job_id, f"LIST enumerate: {len(chapters)} chapters across {pages} page(s)")
        return chapters

    def _same_story(self, url: str, story_root: str, base_host: str) -> bool:
        """True if url stays on the same host AND under story_root — the
        chain-mode drift guard (stops 'next' links into a different story)."""
        from urllib.parse import urlparse
        p = urlparse(url)
        if (p.netloc or base_host) != base_host:
            return False
        return p.path.startswith(story_root)

    def _run_recipe_crawl(self, job_id: str, start_url: str, recipe: dict,
                          speed: float, max_chapters: int, headers: dict,
                          resume_from: int = 0):
        """AI-driven crawl: build the chapter URL list from the recipe (LIST or
        CHAIN mode), then fetch each chapter's content statically and save.

        same_story guard (story_root) is enforced in BOTH modes so the crawl can
        never drift into a different story. On repeated content failures it flags
        the job 'needs_attention' and wakes CrawlStoriesAgent to self-heal.

        resume_from: skip this many already-saved chapters (self-heal continues
        from the checkpoint instead of restarting at chapter 1).
        """
        from urllib.parse import urlparse

        mode = recipe.get("mode", "list")
        story_root = recipe.get("story_root") or "/"
        content_sel = recipe.get("content_selector")
        title_sel = recipe.get("title_selector", "h1")
        base_host = urlparse(start_url).netloc
        story_title = recipe.get("story_title") or ""

        # ── Build the chapter list (NO LLM — code enumerates) ──
        if mode == "list":
            chapters = self._enumerate_list_mode(job_id, start_url, recipe)
            if not chapters:
                self._update_job(job_id, status="needs_attention",
                                 message="LIST mode found 0 chapters — recipe selector/pattern likely wrong")
                _dbg(job_id, "LIST mode: 0 chapters → needs_attention")
                return
        else:
            chapters = None  # CHAIN mode walks next-links lazily below

        # ── Resolve story folder/title from the first chapter page ──
        first_url = (chapters[0]["url"] if chapters
                     else recipe.get("first_chapter_url") or start_url)
        first_html = render_page_or_get(first_url, headers or _CHAPTER_HEADERS)
        if not first_html:
            self._update_job(job_id, status="needs_attention",
                             message=f"Could not fetch first chapter {first_url}")
            return
        fsoup = BeautifulSoup(first_html, "html.parser")
        if not story_title:
            t = fsoup.select_one(title_sel)
            raw = t.get_text(strip=True) if t else ""
            story_title = re.sub(r'\s*(Chương|Chapter|Quyển)\s*\d+.*$', '', raw, flags=re.I).strip() or "Story"
        story_folder = re.sub(r'[\\/*?:"<>|]', "", story_title).strip() or "Untitled_Story"
        save_dir = os.path.join(DATA_DIR, "stories", story_folder)
        os.makedirs(save_dir, exist_ok=True)
        self._register_story_meta(story_folder, story_title, start_url)
        total = len(chapters) if chapters else max_chapters
        self._update_job(job_id, story_title=story_title, total_chapters=total,
                         current_chapter=0, message="Starting crawl...")
        _dbg(job_id, f"[RECIPE/{mode}] story='{story_title}' total={total}")

        # ── Crawl loop ──
        # resume_from: continue after the checkpoint. LIST mode skips that many
        # entries; CHAIN mode can't random-access so it re-walks but only saves
        # past the checkpoint (filenames are index-based → no dupes/gaps).
        count = resume_from if resume_from else 0
        empty_streak = 0
        if chapters and resume_from:
            chapter_iter = iter(chapters[resume_from:])
            _dbg(job_id, f"[RECIPE] resuming LIST from chapter {resume_from+1}")
        else:
            chapter_iter = iter(chapters) if chapters else None
        current_url = first_url
        while count < max_chapters:
            if self._get_job_status(job_id) == "cancelled":
                self._update_job(job_id, message="Cancelled by user.")
                _dbg(job_id, "Cancelled by user")
                return

            if chapter_iter is not None:
                try:
                    current_url = next(chapter_iter)["url"]
                except StopIteration:
                    break
            # CHAIN drift guard: never follow a link outside the story
            if not self._same_story(current_url, story_root, base_host):
                _dbg(job_id, f"[RECIPE] drift blocked: {current_url[:70]} not under {story_root}")
                break

            html = render_page_or_get(current_url, headers)
            if not html:
                empty_streak += 1
                if empty_streak >= 3:
                    self._update_job(job_id, status="needs_attention",
                                     message=f"3 fetch failures in a row near chapter {count+1} ({current_url})")
                    _dbg(job_id, "3 fetch failures → needs_attention")
                    return
                if chapter_iter is not None:
                    continue
                break
            soup = BeautifulSoup(html, "html.parser")
            cdiv = soup.select_one(content_sel) if content_sel else None
            text = ""
            if cdiv:
                for s in cdiv(["script", "style", "iframe"]):
                    s.decompose()
                text = cdiv.get_text(separator="\n\n").strip()

            if len(text) < 100:
                empty_streak += 1
                _dbg(job_id, f"[RECIPE] empty/short content #{count+1} (streak={empty_streak})")
                if empty_streak >= 3:
                    # Self-heal trigger (Phase 4): selector likely broke mid-crawl
                    self._flag_needs_attention(job_id, current_url, recipe, count,
                                               "content_selector returned empty for 3 chapters in a row")
                    return
                if chapter_iter is not None:
                    continue
                break
            empty_streak = 0

            t_tag = soup.select_one(title_sel)
            chap_title = t_tag.get_text(strip=True) if t_tag else f"Chapter {count+1}"
            filename = f"{count+1:03d}_{re.sub(r'[\\/*?:\"<>|]', '', chap_title)[:50]}.txt"
            with open(os.path.join(save_dir, filename), "w", encoding="utf-8") as f:
                f.write(text)
            count += 1
            self._update_job(job_id, current_chapter=count, message=f"Crawled: {chap_title}")
            _dbg(job_id, f"[RECIPE] saved #{count}: {chap_title[:40]}")

            # CHAIN mode: find next chapter link, validate same-story
            if chapter_iter is None:
                nxt = None
                nsel = recipe.get("next_chapter_selector")
                if nsel:
                    n = soup.select_one(nsel)
                    if n and n.get("href"):
                        nxt = urljoin(current_url, n["href"])
                if not nxt or not self._same_story(nxt, story_root, base_host):
                    _dbg(job_id, "[RECIPE/chain] no valid same-story next link → stop")
                    break
                current_url = nxt
            time.sleep(speed)

        self._update_job(job_id, status="completed",
                         message=f"Completed. Saved {count} chapters.")
        self._finalize_story_meta(story_folder, count)
        _dbg(job_id, f"[RECIPE] Completed. {count} chapters.")

    def _register_story_meta(self, story_folder: str, story_title: str, src: str):
        try:
            from core.database import StoryMeta
            db = get_db_session()
            try:
                if not db.query(StoryMeta).filter(StoryMeta.story_id == story_folder).first():
                    db.add(StoryMeta(story_id=story_folder, title=story_title,
                                     source_url=src, chapter_count=0))
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning("[CRAWL_POLLER] register meta failed: %s", e)

    def _finalize_story_meta(self, story_folder: str, count: int):
        try:
            from core.database import StoryMeta
            db = get_db_session()
            try:
                m = db.query(StoryMeta).filter(StoryMeta.story_id == story_folder).first()
                if m:
                    m.chapter_count = count
                    m.updated_at = datetime.now().timestamp()
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning("[CRAWL_POLLER] finalize meta failed: %s", e)

    def _flag_needs_attention(self, job_id, url, recipe, count, reason):
        """Self-heal trigger: worker hit an anomaly it can't resolve by itself.

        1. Persist checkpoint (chapter index, current recipe, stuck URL) so a
           later resume continues instead of restarting.
        2. Mark status='needs_attention' + a human-readable message → the crawl
           banner (useCrawlStatus) turns to a warning state.
        3. Push an activity_stream event so the UI updates instantly (no poll
           wait) — the user SEES the problem.
        4. Schedule a CrawlStoriesAgent turn on the main loop to diagnose + fix
           the recipe (event-driven, agent_lock auto-queues if Jarvis is busy).
        """
        checkpoint = {
            "stuck_url": url,
            "stuck_index": count,          # 0-based: chapters 0..count-1 are saved
            "recipe": recipe,
            "reason": reason,
        }
        # 1 + 2: persist checkpoint into params.checkpoint + flip status
        self._update_job(
            job_id,
            status="needs_attention",
            message=f"Kẹt ở chương {count+1}: {reason}",
            **self._merge_checkpoint(job_id, checkpoint),
        )
        _dbg(job_id, f"needs_attention @ ch{count+1}: {reason}")

        # 3: instant UI notify (activity_stream is sync → safe from this thread)
        try:
            from services.activity_stream import activity_stream_manager
            activity_stream_manager.broadcast({
                "agent_name": "CrawlStoriesAgent",
                "event_type": "crawl_anomaly",
                "message": f"Crawl gặp vấn đề ở chương {count+1}: {reason}",
                "run_id": job_id,
                "timestamp": time.time(),
                "data": {"job_id": job_id, "stuck_index": count, "reason": reason},
            })
        except Exception as e:
            logger.warning("[CRAWL_POLLER] activity broadcast failed: %s", e)

        # 4: wake CrawlStoriesAgent to self-heal (thread → main loop)
        self._schedule_self_heal(job_id, checkpoint)

    def _merge_checkpoint(self, job_id: str, checkpoint: dict) -> dict:
        """Return a {'params': <json>} update that preserves existing params and
        adds the checkpoint, for _update_job(**...)."""
        try:
            db = get_db_session()
            try:
                job = db.query(CrawlJob).filter(CrawlJob.job_id == job_id).first()
                params = json.loads(job.params) if (job and job.params) else {}
            finally:
                db.close()
        except Exception:
            params = {}
        params["checkpoint"] = checkpoint
        return {"params": json.dumps(params, ensure_ascii=False)}

    def _schedule_self_heal(self, job_id: str, checkpoint: dict):
        """Hand a self-heal agent turn to the main event loop from this worker
        thread. CrawlStoriesAgent wakes, diagnoses, and (via verify_chapters +
        resume_crawl tools) fixes the recipe. _agent_lock serialises it behind
        any in-flight user turn — no race, no lost work."""
        if self._loop is None:
            _dbg(job_id, "self-heal skipped: no event loop ref")
            return
        reason = checkpoint.get("reason", "unknown")
        stuck_idx = checkpoint.get("stuck_index", 0)
        stuck_url = checkpoint.get("stuck_url", "")
        recipe = checkpoint.get("recipe", {})
        payload = (
            f"[CRAWL SELF-HEAL] Crawl job {job_id} bị kẹt ở chương {stuck_idx+1}: {reason}\n"
            f"URL kẹt: {stuck_url}\n"
            f"Recipe hiện tại: {json.dumps(recipe, ensure_ascii=False)}\n\n"
            f"HÃY: render lại trang kẹt (detect_story_structure), chẩn đoán selector/pattern sai, "
            f"chạy verify_chapters với recipe đã sửa. Nếu OK → gọi resume_crawl(job_id='{job_id}', "
            f"recipe_patch=<phần sửa>). Nếu thật sự đã hết truyện ở chương {stuck_idx} → "
            f"gọi resume_crawl(job_id='{job_id}', recipe_patch={{}}, mark_done=true). "
            f"Nếu site đổi cấu trúc hẳn / bị chặn → báo người dùng."
        )

        async def _run():
            try:
                from services.shared_state import session_service, agent_app
                await session_service.resume_and_send(
                    agent_app, payload, session_id=None, agent_name="CrawlStoriesAgent",
                )
            except Exception as e:
                logger.error("[CRAWL_POLLER] self-heal turn failed for %s: %s", job_id[:8], e)

        try:
            asyncio.run_coroutine_threadsafe(_run(), self._loop)
            _dbg(job_id, "self-heal agent turn scheduled")
        except Exception as e:
            logger.error("[CRAWL_POLLER] failed to schedule self-heal: %s", e)

    def _crawl_worker(self, job_id: str, start_url: str, params: dict):
        """Main crawl logic — runs in a thread within FastAPI process."""
        content_selector = params.get("content_selector")
        title_selector = params.get("title_selector", "h1")
        next_selector = params.get("next_selector")
        speed = params.get("speed", 2.0)
        max_chapters = params.get("max_chapters", 2000)
        recipe = params.get("recipe")  # AI-generated crawl recipe (new path)
        resume_from = params.get("resume_from", 0)  # self-heal: skip already-saved

        _dbg(job_id, f"Worker started: {start_url}" + (" [RECIPE]" if recipe else " [legacy]"))
        
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            
            # Check cancellation early
            if self._get_job_status(job_id) == "cancelled":
                _dbg(job_id, "Cancelled before start")
                return

            # ── AI-driven path: a recipe was supplied → enumerate + crawl by it,
            #    bypassing the legacy provider-autodetect / regex chapter-1 logic.
            if recipe:
                self._run_recipe_crawl(job_id, start_url, recipe, speed, max_chapters, headers,
                                       resume_from=resume_from)
                return

            # --- Provider auto-detection ---
            # Import StoryConfigManager from tools module (safe, no MCP dependency)
            try:
                import sys
                backend_dir = os.path.dirname(os.path.dirname(__file__))
                if backend_dir not in sys.path:
                    sys.path.insert(0, backend_dir)
                from tools.story_server import StoryConfigManager, _analyze_story_pattern_impl
                
                provider = StoryConfigManager.get_provider_for_url(start_url)
                
                if not provider:
                    _dbg(job_id, "No known provider, auto-discovering...")
                    try:
                        analysis_json = _analyze_story_pattern_impl(start_url)
                        analysis = json.loads(analysis_json)
                        if analysis and analysis.get("content"):
                            from urllib.parse import urlparse
                            domain = urlparse(start_url).netloc
                            name = domain.split(':')[0]
                            provider = {
                                "domain": domain, "name": name.capitalize(),
                                "selectors": {
                                    "content": analysis.get("content"),
                                    "title": analysis.get("title", "h1"),
                                    "next_chapter": analysis.get("next_chapter"),
                                },
                                "trust_level": "auto-learned",
                            }
                            StoryConfigManager.save_provider(provider)
                            _dbg(job_id, f"Auto-discovered provider: {name}")
                    except Exception as e:
                        _dbg(job_id, f"Auto-discovery failed: {e}")
                
                if provider:
                    # Verify if not verified
                    if provider.get("trust_level") != "verified":
                        sels = provider.get("selectors", {})
                        c_sel = sels.get("content")
                        if c_sel:
                            _vs, test_text = get_capped_text(
                                start_url, headers=headers, timeout=10,
                            )
                            test_soup = BeautifulSoup(test_text, "html.parser")
                            test_div = test_soup.select_one(c_sel)
                            if test_div and len(test_div.get_text(strip=True)) > 200:
                                provider["trust_level"] = "verified"
                                StoryConfigManager.save_provider(provider)
                                _dbg(job_id, "Provider verified")
                            else:
                                provider = None
                                _dbg(job_id, "Provider verification failed")
                        else:
                            provider = None
                    
                    if provider and provider.get("selectors"):
                        sels = provider["selectors"]
                        if sels.get("content"): content_selector = sels["content"]
                        if sels.get("title"): title_selector = sels["title"]
                        if sels.get("next_chapter"): next_selector = sels["next_chapter"]
            except ImportError:
                _dbg(job_id, "Could not import StoryConfigManager, using params as-is")
            
            # --- Get chapter list ---
            chapters = []
            try:
                from tools.story_server import _get_story_chapters_impl
                chapters = _get_story_chapters_impl(start_url)
                _dbg(job_id, f"Found {len(chapters)} chapters from list")
                
                if chapters:
                    # Find Chapter 1
                    # TODO(i18n): VN literals — regex patterns match Vietnamese chapter titles in scraped HTML
                    c1 = next((c for c in chapters if re.search(
                        r'(chương|chapter)\s+0*1(\s+:|$|\D)', c['title'], re.I
                    ) or re.search(r'(mở đầu|văn án)', c['title'], re.I)), None)
                    
                    if c1 and c1['url'] != start_url:
                        _dbg(job_id, f"Switching to Chapter 1: {c1['url'][:80]}")
                        start_url = c1['url']
                    elif not c1 and chapters:
                        c1 = chapters[0]
                        if c1['url'] != start_url:
                            _dbg(job_id, f"Using first chapter: {c1['url'][:80]}")
                            start_url = c1['url']
            except Exception as e:
                _dbg(job_id, f"Chapter list detection failed: {e}")
            
            # Cancel check
            if self._get_job_status(job_id) == "cancelled":
                _dbg(job_id, "Cancelled during setup")
                return
            
            # --- Determine story name ---
            _ts, resp_text = get_capped_text(start_url, headers=headers, timeout=10)
            soup = BeautifulSoup(resp_text, "html.parser")
            title_tag = soup.select_one(title_selector)
            full_title = title_tag.get_text(strip=True) if title_tag else "Unknown Story"
            
            story_name = full_title.split("-")[0].strip()
            # TODO(i18n): VN literal — strips Vietnamese chapter suffix from scraped page titles
            story_name = re.sub(r'\s*(Chương|Chapter)\s+\d+.*$', '', story_name, flags=re.I).strip()
            if not story_name:
                story_name = f"Story_{int(time.time())}"
            
            story_folder = re.sub(r'[\\/*?:"<>|]', "", story_name).strip()
            if not story_folder:
                story_folder = "Untitled_Story"
            try:
                save_dir = safe_story_path(Path(DATA_DIR) / "stories", story_folder)
            except ValueError as e:
                # Scraped <title> produced a traversal-like value (eg "..").
                # Don't write outside the sandbox — fall back to a timestamped
                # name and log the rejection so a future debug has evidence.
                _dbg(job_id, f"Unsafe story folder {story_folder!r} rejected: {e}; using timestamped fallback")
                story_folder = f"Untitled_Story_{int(time.time())}"
                save_dir = safe_story_path(Path(DATA_DIR) / "stories", story_folder)
            save_dir.mkdir(parents=True, exist_ok=True)
            save_dir = str(save_dir)  # downstream os.path.join() callers expect str
            
            # Register story metadata in DB (replaces legacy metadata.json)
            try:
                from core.database import StoryMeta
                db = get_db_session()
                try:
                    existing_meta = db.query(StoryMeta).filter(
                        StoryMeta.story_id == story_folder
                    ).first()
                    if not existing_meta:
                        new_meta = StoryMeta(
                            story_id=story_folder,
                            title=story_name,
                            source_url=start_url,
                            chapter_count=0,
                        )
                        db.add(new_meta)
                        db.commit()
                finally:
                    db.close()
            except Exception as e:
                _dbg(job_id, f"Warning: Failed to register story meta in DB: {e}")
            
            self._update_job(job_id, story_title=story_name, total_chapters=max_chapters, 
                           current_chapter=0, message="Starting crawl...")
            _dbg(job_id, f"Story: {story_name}, chapters={len(chapters)}")
            
            # --- Crawl loop ---
            current_url = start_url
            count = 0
            
            # List Mode vs Chain Mode
            chapter_iterator = None
            if chapters:
                start_index = -1
                for i, c in enumerate(chapters):
                    if c['url'].strip('/') == start_url.strip('/'):
                        start_index = i
                        break
                if start_index != -1:
                    _dbg(job_id, f"LIST MODE from index {start_index}/{len(chapters)}")
                    chapter_iterator = iter(chapters[start_index:])
                else:
                    _dbg(job_id, "CHAIN MODE (start URL not in chapter list)")
            
            while count < max_chapters:
                # Cancel check (from DB — independent of MCP subprocess)
                if self._get_job_status(job_id) == "cancelled":
                    _dbg(job_id, "Cancelled by user")
                    self._update_job(job_id, message="Cancelled by user.")
                    break
                
                if chapter_iterator:
                    try:
                        chapter_node = next(chapter_iterator)
                        current_url = chapter_node['url']
                    except StopIteration:
                        _dbg(job_id, "End of chapter list")
                        break
                
                _dbg(job_id, f"Fetching #{count+1}: {current_url[:80]}")
                
                # Fetch with retry on 429. Body is streamed + capped at
                # MAX_CHAPTER_BYTES so a hostile/oversized chapter page
                # can't fill RAM or disk on its own.
                resp_status: int | None = None
                resp_text = ""
                for attempt in range(4):
                    try:
                        resp_status, resp_text = get_capped_text(
                            current_url, headers=headers, timeout=10,
                        )
                        if resp_status == 429 and attempt < 3:
                            _dbg(job_id, f"429 Too Many Requests, retry {attempt+1}/3 in 5s")
                            time.sleep(5)
                            continue
                        break
                    except Exception as e:
                        _dbg(job_id, f"Request failed: {e}")
                        resp_status = None
                        break

                if resp_status is None:
                    break
                if resp_status != 200:
                    _dbg(job_id, f"HTTP {resp_status} for {current_url[:60]}")
                    if chapter_iterator:
                        continue
                    break

                soup = BeautifulSoup(resp_text, "html.parser")
                
                # Extract content
                content_div = soup.select_one(content_selector) if content_selector else None
                if not content_div:
                    if chapter_iterator:
                        continue
                    break
                
                for s in content_div(["script", "style", "iframe"]):
                    s.decompose()
                text = content_div.get_text(separator="\n\n").strip()
                
                # Fallback content extraction
                if not text and content_selector:
                    for sel in [content_selector, "div.content", "div#content", "div.chapter-c"]:
                        c_tag = soup.select_one(sel)
                        if c_tag:
                            text = c_tag.get_text("\n", strip=True)
                            break
                
                if not text:
                    ps = soup.select("p")
                    if len(ps) > 5:
                        text = "\n".join([p.get_text(strip=True) for p in ps])
                
                # Title
                t_tag = soup.select_one(title_selector)
                chap_title = t_tag.get_text(strip=True) if t_tag else f"Chapter {count+1}"
                
                # Save
                filename = f"{count+1:03d}_{re.sub(r'[\\\\/*?:\"<>|]', '', chap_title)[:50]}.txt"
                with open(os.path.join(save_dir, filename), "w", encoding="utf-8") as f:
                    f.write(text)
                
                count += 1
                self._update_job(job_id, current_chapter=count, message=f"Crawled: {chap_title}")
                _dbg(job_id, f"Saved #{count}: {chap_title}")
                
                # Next link (chain mode only)
                if not chapter_iterator:
                    next_url = None
                    if next_selector:
                        n = soup.select_one(next_selector)
                        if n and n.get("href"):
                            next_url = n.get("href")
                    
                    if not next_url:
                        candidates = soup.select(
                            "a.next, a.chap-next, a#next_chap, a.btn-next, "
                            "a[title*='sau'], a[title*='next']"
                        )
                        if candidates:
                            next_url = candidates[0].get("href")
                        else:
                            for a in soup.find_all("a"):
                                t = a.get_text(strip=True).lower()
                                # TODO(i18n): VN literal — matches Vietnamese "next chapter" link text on scraped pages
                                if any(kw in t for kw in ["chương sau", "tiếp", "next", "chap sau"]):
                                    next_url = a.get("href")
                                    break
                    
                    if next_url:
                        if not next_url.startswith("http"):
                            next_url = urljoin(current_url, next_url)
                        current_url = next_url
                    else:
                        _dbg(job_id, "No next URL found, stopping")
                        break
                
                _dbg(job_id, f"Sleeping {speed}s...")
                time.sleep(speed)
            
            # Done
            _dbg(job_id, f"Completed. {count} chapters saved.")
            self._update_job(job_id, status="completed", 
                           message=f"Completed. Saved {count} chapters.")
            
            # Update story_meta chapter count
            try:
                from core.database import StoryMeta
                db = get_db_session()
                try:
                    meta = db.query(StoryMeta).filter(
                        StoryMeta.story_id == story_folder
                    ).first()
                    if meta:
                        meta.chapter_count = count
                        import datetime
                        meta.updated_at = datetime.datetime.now().timestamp()
                        db.commit()
                finally:
                    db.close()
            except Exception as e:
                _dbg(job_id, f"Warning: Failed to update story meta: {e}")
            
        except Exception as e:
            import traceback
            _dbg(job_id, f"EXCEPTION: {e}\n{traceback.format_exc()}")
            self._update_job(job_id, status="failed", message=str(e)[:500])
        finally:
            self._active_threads.pop(job_id, None)
