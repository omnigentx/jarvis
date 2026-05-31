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


class CrawlPoller:
    """Background poller that picks up pending crawl jobs from DB."""
    
    POLL_INTERVAL = 5  # seconds
    
    def __init__(self):
        self._running = False
        self._active_threads: dict[str, threading.Thread] = {}
    
    async def start(self):
        """Main loop — runs forever within FastAPI lifespan."""
        self._running = True
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
    
    def _crawl_worker(self, job_id: str, start_url: str, params: dict):
        """Main crawl logic — runs in a thread within FastAPI process."""
        content_selector = params.get("content_selector")
        title_selector = params.get("title_selector", "h1")
        next_selector = params.get("next_selector")
        speed = params.get("speed", 2.0)
        max_chapters = params.get("max_chapters", 2000)
        
        _dbg(job_id, f"Worker started: {start_url}")
        
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            
            # Check cancellation early
            if self._get_job_status(job_id) == "cancelled":
                _dbg(job_id, "Cancelled before start")
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
                            test_resp = requests.get(start_url, headers=headers, timeout=10)
                            test_soup = BeautifulSoup(test_resp.text, "html.parser")
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
            resp = requests.get(start_url, headers=headers, timeout=10)
            soup = BeautifulSoup(resp.text, "html.parser")
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
                
                # Fetch with retry on 429
                resp = None
                for attempt in range(4):
                    try:
                        resp = requests.get(current_url, headers=headers, timeout=10)
                        if resp.status_code == 429:
                            if attempt < 3:
                                _dbg(job_id, f"429 Too Many Requests, retry {attempt+1}/3 in 5s")
                                time.sleep(5)
                                continue
                        break
                    except Exception as e:
                        _dbg(job_id, f"Request failed: {e}")
                        resp = None
                        break
                
                if not resp:
                    break
                if resp.status_code != 200:
                    _dbg(job_id, f"HTTP {resp.status_code} for {current_url[:60]}")
                    if chapter_iterator:
                        continue
                    break
                
                soup = BeautifulSoup(resp.text, "html.parser")
                
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
