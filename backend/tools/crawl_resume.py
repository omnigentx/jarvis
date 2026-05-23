#!/usr/bin/env python3
"""Standalone crawl worker — spawned by story_server atexit handler.

Reads job state from DB, resumes crawl from last saved chapter.
Runs as an independent process, not tied to any MCP lifecycle.
"""
import sys
import os
import json
import re
import time
import logging

# Setup paths
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _backend_dir)
os.chdir(_backend_dir)

from core.database import get_db_session, CrawlJob, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s - crawl_resume - %(levelname)s - %(message)s")
logger = logging.getLogger("crawl_resume")


def resume_crawl(job_id: str, params_json: str):
    """Resume a crawl job from where it left off."""
    import requests as http_req
    from urllib.parse import urljoin
    from bs4 import BeautifulSoup
    from tools.story_server import StoryConfigManager, _get_story_chapters_impl

    params = json.loads(params_json)
    content_selector = params.get("content_selector")
    title_selector = params.get("title_selector", "h1")
    next_selector = params.get("next_selector")
    speed = params.get("speed", 2.0)
    max_chapters = params.get("max_chapters", 2000)

    DATA_DIR = os.path.join(_backend_dir, "data")

    # Read current state from DB
    db = get_db_session()
    job = db.query(CrawlJob).filter(CrawlJob.job_id == job_id).first()
    if not job:
        logger.error(f"Job {job_id} not found in DB")
        return
    if job.status != "running":
        logger.info(f"Job {job_id} status is '{job.status}', skipping")
        db.close()
        return

    start_url = job.start_url
    current_chapter = job.current_chapter or 0
    story_title = job.story_title or "Unknown"
    db.close()

    logger.info(f"[{job_id[:8]}] Resuming '{story_title}' from chapter {current_chapter}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    # Get provider selectors
    provider = StoryConfigManager.get_provider_for_url(start_url)
    if provider:
        sels = provider.get("selectors", {})
        if not content_selector and sels.get("content"):
            content_selector = sels["content"]
        if sels.get("title"):
            title_selector = sels["title"]
        if not next_selector and sels.get("next_chapter"):
            next_selector = sels["next_chapter"]

    # Get chapter list
    chapters = []
    try:
        chapters = _get_story_chapters_impl(start_url)
        logger.info(f"[{job_id[:8]}] Got {len(chapters)} chapters from list")
    except Exception as e:
        logger.warning(f"[{job_id[:8]}] Chapter list failed: {e}")

    # Find story folder
    story_folder = re.sub(r'[\\/*?:"<>|]', "", story_title).strip() or "Untitled"
    save_dir = os.path.join(DATA_DIR, "stories", story_folder)
    os.makedirs(save_dir, exist_ok=True)

    # Set up chapter iterator, skip already-crawled chapters
    chapter_iterator = None
    current_url = start_url
    if chapters:
        # Find start_url in list
        start_index = -1
        for i, c in enumerate(chapters):
            if c["url"].strip("/") == start_url.strip("/"):
                start_index = i
                break
        if start_index != -1:
            # Skip to current_chapter (already downloaded)
            resume_index = start_index + current_chapter
            if resume_index < len(chapters):
                chapter_iterator = iter(chapters[resume_index:])
                logger.info(f"[{job_id[:8]}] LIST MODE resuming from index {resume_index}")
            else:
                logger.info(f"[{job_id[:8]}] Already completed all chapters")
                _update_status(job_id, "completed", current_chapter, f"Completed. Saved {current_chapter} chapters.")
                return

    def _update_status(jid, status=None, current=None, message=None):
        try:
            db2 = get_db_session()
            j = db2.query(CrawlJob).filter(CrawlJob.job_id == jid).first()
            if j:
                if status: j.status = status
                if current is not None: j.current_chapter = current
                if message: j.message = message
            db2.commit()
            db2.close()
        except Exception as e:
            logger.warning(f"DB update failed: {e}")

    count = current_chapter

    try:
        while count < max_chapters:
            # Check cancel
            try:
                db3 = get_db_session()
                j = db3.query(CrawlJob).filter(CrawlJob.job_id == job_id).first()
                if j and j.status == "cancelled":
                    logger.info(f"[{job_id[:8]}] Cancelled")
                    db3.close()
                    return
                db3.close()
            except:
                pass

            if chapter_iterator:
                try:
                    node = next(chapter_iterator)
                    current_url = node["url"]
                except StopIteration:
                    break
            
            # Fetch
            resp = None
            for attempt in range(4):
                try:
                    resp = http_req.get(current_url, headers=headers, timeout=10)
                    if resp.status_code == 429 and attempt < 3:
                        time.sleep(5)
                        continue
                    break
                except Exception as e:
                    logger.error(f"[{job_id[:8]}] Fetch error: {e}")
                    resp = None
                    break

            if not resp or resp.status_code != 200:
                if chapter_iterator:
                    continue
                else:
                    break

            soup = BeautifulSoup(resp.text, "html.parser")

            # Extract content
            text = ""
            if content_selector:
                content_div = soup.select_one(content_selector)
                if content_div:
                    for s in content_div(["script", "style", "iframe"]):
                        s.decompose()
                    text = content_div.get_text(separator="\n\n").strip()

            if not text:
                if chapter_iterator:
                    continue
                else:
                    break

            # Title
            ct = soup.select_one(title_selector)
            chap_title = ct.get_text(strip=True) if ct else f"Chapter {count+1}"

            # Save
            fname = f"{count+1:03d}_{re.sub(r'[\\\\/*?:\"<>|]', '', chap_title)[:50]}.txt"
            with open(os.path.join(save_dir, fname), "w", encoding="utf-8") as f:
                f.write(text)

            count += 1
            _update_status(job_id, current=count, message=f"Crawled: {chap_title}")
            logger.info(f"[{job_id[:8]}] Crawled {count}: {chap_title}")

            # Chain mode next URL
            if not chapter_iterator:
                next_url = None
                if next_selector:
                    n = soup.select_one(next_selector)
                    if n and n.get("href"):
                        next_url = n["href"]
                if not next_url:
                    for a in soup.find_all("a"):
                        t = a.get_text(strip=True).lower()
                        # TODO(i18n): VN literal — matches Vietnamese story-site "next chapter" link text
                        if any(kw in t for kw in ["chương sau", "tiếp", "next", "chap sau"]):
                            next_url = a.get("href")
                            break
                if next_url:
                    if not next_url.startswith("http"):
                        next_url = urljoin(current_url, next_url)
                    current_url = next_url
                else:
                    break

            time.sleep(speed)

        _update_status(job_id, "completed", count, f"Completed. Saved {count} chapters.")
        logger.info(f"[{job_id[:8]}] Completed. {count} chapters.")

    except Exception as e:
        logger.error(f"[{job_id[:8]}] FAILED: {e}")
        _update_status(job_id, "failed", message=str(e))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: crawl_resume.py <job_id> <params_json>")
        sys.exit(1)
    resume_crawl(sys.argv[1], sys.argv[2])
