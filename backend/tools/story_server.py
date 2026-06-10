import logging
import requests
import json
import os
import re
import unicodedata
from pathlib import Path
from bs4 import BeautifulSoup

# Bootstrap sys.path BEFORE importing helpers.*: when this file is launched as a
# script (python tools/story_server.py) by the MCP runtime, sys.path[0] is the
# tools/ dir, not backend/, so `helpers` is unresolvable and the server crashes
# at startup with ModuleNotFoundError (shows up as "story-server Failed" in UI).
# Inserting backend/ here — above the helpers imports — is the fix.
import sys
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from helpers.crawl_markers import (
    CHAPTER_HEADING_RE,
    CHAPTER_HREF_RE,
    NEXT_PAGE_LINK_RE,
    SITE_TITLE_SUFFIX_RE,
)
from helpers.http_safety import get_capped_text
from helpers.path_safety import safe_story_path
from mcp.server.fastmcp import FastMCP
from typing import List, Dict, Optional
import time
from urllib.parse import urljoin
import uuid
import threading
from dotenv import load_dotenv

# Logging — inherits config from centralized logging_config
logger = logging.getLogger("story_server")

# Constants
# DATA_DIR: absolute path to backend/data/ — works in MCP subprocess context
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# Load .env from backend directory (_backend_dir set during sys.path bootstrap above)
load_dotenv(os.path.join(_backend_dir, ".env"))

# NOTE: Crawl execution is decoupled from MCP subprocess.
# crawl_story() only inserts a pending job into DB.
# CrawlPoller (in FastAPI process) picks up and executes crawl jobs.
# This ensures crawl continues even when MCP subprocess is killed.

# Initialize FastMCP server
mcp = FastMCP("StoryServer")


def _get_db():
    """Get DB session for story_server (MCP subprocess).
    Uses core.database which resolves DB path relative to CWD."""
    from core.database import get_db_session
    return get_db_session()


class StoryConfigManager:
    """Story-website provider config, persisted to SQLite (table
    story_providers). Replaces the legacy story_providers.json file."""

    @staticmethod
    def load_providers() -> List[Dict]:
        """Load all providers from the DB and return the legacy API
        shape (``list[dict]``) for back-compat with existing callers."""
        from core.database import StoryProvider
        db = _get_db()
        try:
            rows = db.query(StoryProvider).all()
            result = []
            for r in rows:
                p = {
                    "domain": r.domain,
                    "name": r.name,
                    "selectors": json.loads(r.selectors_json) if r.selectors_json else {},
                    "trust_level": r.trust_level or "auto-learned",
                }
                if r.search_url:
                    p["search_url"] = r.search_url
                if r.list_selector:
                    p["list_selector"] = r.list_selector
                if r.title_selector:
                    p["title_selector"] = r.title_selector
                if r.known_stories_json:
                    p["known_stories"] = json.loads(r.known_stories_json)
                else:
                    p["known_stories"] = []
                result.append(p)
            return result
        except Exception as e:
            logger.error(f"Error loading providers from DB: {e}")
            return []
        finally:
            db.close()

    @staticmethod
    def save_provider(provider: Dict):
        """Upsert provider theo domain."""
        from core.database import StoryProvider
        from datetime import datetime
        db = _get_db()
        try:
            existing = db.query(StoryProvider).filter(
                StoryProvider.domain == provider['domain']
            ).first()

            selectors_json = json.dumps(provider.get("selectors", {}), ensure_ascii=False)
            known_stories_json = json.dumps(provider.get("known_stories", []), ensure_ascii=False)

            if existing:
                existing.name = provider.get("name", existing.name)
                existing.selectors_json = selectors_json
                existing.search_url = provider.get("search_url", existing.search_url)
                existing.list_selector = provider.get("list_selector", existing.list_selector)
                existing.title_selector = provider.get("title_selector", existing.title_selector)
                existing.trust_level = provider.get("trust_level", existing.trust_level)
                existing.known_stories_json = known_stories_json
                existing.updated_at = datetime.now().timestamp()
            else:
                new_provider = StoryProvider(
                    domain=provider["domain"],
                    name=provider.get("name", provider["domain"]),
                    selectors_json=selectors_json,
                    search_url=provider.get("search_url"),
                    list_selector=provider.get("list_selector"),
                    title_selector=provider.get("title_selector"),
                    trust_level=provider.get("trust_level", "auto-learned"),
                    known_stories_json=known_stories_json,
                )
                db.add(new_provider)
            
            db.commit()
        except Exception as e:
            logger.error(f"Error saving provider: {e}")
            db.rollback()
        finally:
            db.close()

    @staticmethod
    def get_provider_for_url(url: str) -> Optional[Dict]:
        """Return the provider whose domain matches this URL, or None."""
        providers = StoryConfigManager.load_providers()
        for p in providers:
            if p['domain'] in url:
                return p
        return None

    @staticmethod
    def add_known_story(domain: str, story_name: str, story_url_full: str = None):
        """Append a story to the provider's known-stories list."""
        from core.database import StoryProvider
        from datetime import datetime
        story_name = story_name.strip()
        if not story_name:
            return
        
        db = _get_db()
        try:
            provider = db.query(StoryProvider).filter(
                StoryProvider.domain == domain
            ).first()
            if not provider:
                return
            
            known = json.loads(provider.known_stories_json) if provider.known_stories_json else []
            
            # Check existence and update
            exists = False
            for i, item in enumerate(known):
                # Handle legacy string entries
                if isinstance(item, str):
                    if item == story_name:
                        exists = True
                        if story_url_full:
                            known[i] = {"title": story_name, "url": story_url_full}
                        break
                elif isinstance(item, dict):
                    if item.get("title") == story_name:
                        exists = True
                        if story_url_full and item.get("url") != story_url_full:
                            item["url"] = story_url_full
                        break

            if not exists:
                known.append({"title": story_name, "url": story_url_full or ""})
                logger.info(f"Prioritization: Added '{story_name}' to {domain}")

            provider.known_stories_json = json.dumps(known, ensure_ascii=False)
            provider.updated_at = datetime.now().timestamp()
            db.commit()
        except Exception as e:
            logger.error(f"Failed to save known story: {e}")
            db.rollback()
        finally:
            db.close()

# --- Helper: Generic Heuristic Extractor ---

def _clean_dom(soup):
    """Refined DOM cleaner to remove noise."""
    # Remove standard noise tags
    for tag in soup(["script", "style", "nav", "header", "footer", "iframe", "noscript", "svg", "button", "input", "form"]):
        tag.decompose()
        
    # Remove by Class/ID patterns (Sidebars, Ads, Comments, Menus)
    # Using specific keyword lists
    noise_patterns = re.compile(r"sidebar|comment|related|ads|menu|navigation|footer|copyright|author-box|share|popup|modal", re.I)
    
    tags_to_check = list(soup.find_all(['div', 'aside', 'ul', 'section']))
    for tag in tags_to_check:
        try:
            # Check ID
            tid = tag.get("id")
            if tid and isinstance(tid, str) and noise_patterns.search(tid):
                tag.decompose()
                continue
            
            # Check Class
            tcls = tag.get("class")
            if tcls:
                class_str = " ".join(tcls)
                if noise_patterns.search(class_str):
                    tag.decompose()
                    continue
        except Exception:
            pass

def heuristic_extract(html: str) -> tuple[str, Optional[Dict]]:
    """
    Attempt to extract story content.
    Returns: (content_text, detected_metadata_dict)
    """
    soup = BeautifulSoup(html, "html.parser")
    
    # 1. Clean DOM
    _clean_dom(soup)
        
    # 2. Score potential content divs
    candidates = []
    for tag in soup.find_all(['div', 'article', 'section']):
        # [REFACTORED] Density-based scoring
        # Calculate standard metrics
        p_count = len(tag.find_all('p')) 
        # Deep text length (strip whitespace)
        text = tag.get_text(separator=" ", strip=True)
        text_len = len(text)
        
        # Skip empty or trivial blocks
        if text_len < 200: 
            continue
            
        # Scoring Formula:
        # Prioritize blocks with many paragraphs (story structure) and significant length.
        score = (p_count * 50) + (text_len / 20)
        
        # Structure Density Boost:
        # If a div has high text content relative to its HTML size, it's likely the main content.
        # (Simplified approximation: we trust p_count + length more)
        
        class_list = tag.get("class", [])
        id_str = tag.get("id", "")
        attr_str = " ".join(class_list) + " " + id_str
        
        # Semantic Boosts (Tie-breakers, not drivers)
        # Use lower multipliers to avoid over-biasing ads/alerts
        if re.search(r'chapter[-_]?c|content|post-content', attr_str, re.I):
            score *= 1.2
        elif re.search(r'chapter|post|entry|text', attr_str, re.I):
            score *= 1.1
            
        # Penalties for "Description", "Summary", "Intro" - Common false positives
        if re.search(r'desc|summary|intro|info|tom-tat|gioi-thieu', attr_str, re.I):
            score *= 0.2
            
        candidates.append((score, tag))
        
    if not candidates:
        return soup.get_text(separator="\n\n").strip(), None
        
    # Get best candidate
    best_score, best_tag = max(candidates, key=lambda x: x[0])
    
    # 3. Derive Selector
    selector = None
    if best_tag.get("id"):
        selector = f"#{best_tag['id']}"
    elif best_tag.get("class"):
        # Use the first class that looks unique or meaningful? 
        # Or just use the dot notation for all classes (risky if common classes used)
        # Let's try to pick a class that contains 'content' or 'chapter' if possible
        classes = best_tag.get("class")
        target_class = next((c for c in classes if 'content' in c or 'chapter' in c), classes[0])
        selector = f"{best_tag.name}.{target_class}"
    
    metadata = {"content_selector": selector} if selector else None

    # Cleaning
    for a in best_tag.find_all("a"):
        a.unwrap()
        
    return best_tag.get_text(separator="\n\n").strip(), metadata


# --- Tools ---

@mcp.tool()
def list_known_stories() -> str:
    """
    List stories previously accessed from the web (history with URLs).
    Returns JSON list with source, title, and URL for each known story.
    """
    providers = StoryConfigManager.load_providers()
    results = []
    
    seen_urls = set()
    
    for p in providers:
        if "known_stories" in p:
            for known in p["known_stories"]:
                # Handle dictionary vs string
                if isinstance(known, dict):
                    title = known.get("title", "")
                    url = known.get("url", "")
                else:
                    title = str(known)
                    url = p['domain']
                
                if not title: continue
                
                # Check duplication by URL (if url is real) or Title (if url is just domain)
                dedup_key = url if (url and url.startswith("http")) else f"{title}||{p['domain']}"

                if dedup_key not in seen_urls:
                    results.append({
                        "source": p['name'], 
                        "title": title,
                        "url": url if (url and url.startswith("http")) else ""
                    })
                    seen_urls.add(dedup_key)

    return json.dumps(results, ensure_ascii=False)


@mcp.tool()
def web_search_stories(query: str) -> str:
    """
    Search for stories on the web using configured providers.
    Returns JSON list of search results with source, title, and URL.
    """
    providers = StoryConfigManager.load_providers()
    results = []
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    # Standard Web Search
    for p in providers:
        if not p.get('search_url'): continue
        
        try:
            search_url = p['search_url'].replace("{query}", requests.utils.quote(query))
            resp = requests.get(search_url, headers=headers, timeout=(3, 8))
            soup = BeautifulSoup(resp.text, "html.parser")
            
            list_sel = p.get('list_selector')
            title_sel = p.get('title_selector')
            
            if list_sel and title_sel:
                items = soup.select(list_sel)
                for item in items:
                    link = item.select_one(title_sel) if title_sel != "self" else item
                    if link and link.has_attr('href'):
                        results.append({
                            "source": p['name'],
                            "title": link.get_text(strip=True),
                            "url": link['href']
                        })
                        if len(results) >= 5: break
            
        except Exception as e:
            logger.error(f"Error searching {p['name']}: {e}")
            continue
            
    return json.dumps(results, ensure_ascii=False)


@mcp.tool()
def get_story_chapters(story_url: str) -> str:
    """
    Get list of chapters from a web story URL.
    Returns JSON with total_chapters, first/last chapters preview, and chapter_1_url.
    NEXT STEP: Call get_story_page_structure(chapter_1_url) to analyze selectors, then test_crawl_chapter, then crawl_story.
    """
    chapters = _get_story_chapters_impl(story_url)
    total = len(chapters)

    # Truncate to first 5 + last 5 to avoid overwhelming response
    if total > 10:
        preview = chapters[:5] + chapters[-5:]
    else:
        preview = chapters

    result = {
        "total_chapters": total,
        "chapters_preview": preview,
        "chapter_1_url": chapters[0]["url"] if chapters else None,
        "next_step": f"Call get_story_page_structure('{chapters[0]['url']}') to find content selectors, then test_crawl_chapter, then crawl_story." if chapters else "No chapters found."
    }
    return json.dumps(result, ensure_ascii=False)

def _get_story_chapters_impl(story_url: str) -> list:
    """Internal implementation of chapter fetching."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    # --- Special Handling for Truyenzing ---
    if "truyenzing" in story_url:
        try:
            # Extract post_id from URL path segment after truyen/ or story/
            # Handles both old format (truyen/22439) and new format (truyen/slug-10298817)
            post_id = None
            path_match = re.search(r'(?:truyen|story)/([^/?]+)', story_url)
            if path_match:
                path_segment = path_match.group(1)
                if path_segment.isdigit():
                    post_id = path_segment
                else:
                    num_match = re.search(r'(\d+)$', path_segment)
                    if num_match:
                        post_id = num_match.group(1)
            if post_id:
                logger.info(f"Detected Truyenzing ID: {post_id}, fetching chapters via AJAX...")
                
                api_url = "https://truyenzing.com/wp-admin/admin-ajax.php"
                data = {
                    "action": "truyen_dsc",
                    "post_id": post_id,
                    "ord": "ASC" 
                }
                
                resp = requests.post(api_url, data=data, headers=headers, timeout=(3, 15))
                resp.raise_for_status()
                
                # Parse the returned HTML list
                soup = BeautifulSoup(resp.text, "html.parser")
                chapters = []
                seen = set()
                
                for a in soup.find_all("a"):
                    href = a.get("href")
                    title = a.get_text(strip=True)
                    if href and title:
                        if not href.startswith("http"):
                            href = "https://truyenzing.com" + href
                        
                        if href not in seen:
                            chapters.append({"title": title, "url": href})
                            seen.add(href)
                
                return chapters
                
        except Exception as e:
            logger.error(f"Truyenzing API failed: {e}")
            # Fallthrough to generic
            
    try:
        # [Strategy] If URL looks like a chapter, try to find parent Story URL first
        target_urls = [story_url]
        
        # Heuristic: Check for common chapter URL patterns
        # e.g. /chuong-1/, /chapter-3/, /c123/
        if re.search(r'/(chuong|chapter|hoi|c)[-_]?\d+', story_url, re.I):
             # Try to strip the chapter part
             # 1. simple strip of last segment if it matches
             parent_url = re.sub(r'/(chuong|chapter|hoi|c)[-_]?\d+.*$', '', story_url, flags=re.I)
             if parent_url != story_url:
                 # Ensure we didn't strip protocol
                 if parent_url.startswith("http"):
                     logger.info(f"Detected potential Chapter URL. Trying parent Story URL first: {parent_url}")
                     target_urls.insert(0, parent_url) # Try parent FIRST
        
        for url in target_urls:
            try:
                current_scan_url = url
                all_scanned_chapters = []
                scanned_urls = set()
                visited_pages = set()
                page_num = 0
                MAX_PAGES = 100 # Allow up to 100 pages of chapters

                while current_scan_url and page_num < MAX_PAGES:
                    if current_scan_url in visited_pages:
                        logger.warning(f"Loop detected for page: {current_scan_url}. Stopping.")
                        break
                    visited_pages.add(current_scan_url)

                    # logger.info(f"Fetching chapter list page {page_num+1}: {current_scan_url}")
                    response = requests.get(current_scan_url, headers=headers, timeout=(3, 15))
                    if response.status_code != 200: break
                    
                    soup = BeautifulSoup(response.text, "html.parser")
                    
                    # Scope to list-chapter if exists
                    container = soup.select_one(".list-chapter, #list-chapter, .chapter-list, #chapter-list, .ds-chuong")
                    if not container:
                        container = soup
                    
                    page_chapters_found = 0
                    all_links = container.find_all("a")
                    
                    for link in all_links:
                        href = link.get("href")
                        text = link.get_text(strip=True)
                        
                        if not href or not text: continue
                        
                        # Heuristic: link text must look like a chapter
                        # (locale markers live in helpers/crawl_markers).
                        if CHAPTER_HEADING_RE.search(text) or CHAPTER_HREF_RE.search(href):
                             # [FIX] Filter out pagination links explicitly
                            if re.search(r'(trang-\d+|page/\d+|/page-\d+)', href, re.I):
                                continue
                            
                            # Normalize URL
                            if not href.startswith("http"):
                                 href = urljoin(current_scan_url, href)

                            if href not in scanned_urls:
                                all_scanned_chapters.append({"title": text, "url": href})
                                scanned_urls.add(href)
                                page_chapters_found += 1
                    
                    # Log only if useful
                    # logger.info(f"Found {page_chapters_found} chapters on page {page_num+1}")
                    
                    # Find Next Page
                    # Common patterns: .pagination .active + li a, arrow icon, or text "Trang sau"
                    next_link = soup.select_one(".pagination li.active + li a, .pagination .next a, a[rel='next']")
                    
                    # Fallback for TruyenFull: check for specific symbols or text if selector fails
                    if not next_link:
                        next_link = soup.find("a", string=NEXT_PAGE_LINK_RE)

                    if next_link and next_link.get("href"):
                        next_href = next_link.get("href")
                        if "javascript" in next_href: 
                            current_scan_url = None
                        else:
                            if not next_href.startswith("http"):
                                next_href = urljoin(current_scan_url, next_href)
                            
                            # Loop protection
                            if next_href == current_scan_url or next_href in scanned_urls: 
                                current_scan_url = None
                            else:
                                current_scan_url = next_href
                    else:
                        current_scan_url = None
                        
                    page_num += 1
                
                # Check results for this target_url
                if len(all_scanned_chapters) > 2: 
                    logger.info(f"Successfully scraped {len(all_scanned_chapters)} chapters from {url} (across {page_num} pages)")
                    return all_scanned_chapters
                    
            except Exception as ex:
                logger.warning(f"Failed to scrape {url}: {ex}")
                continue
                    
            except Exception as ex:
                logger.warning(f"Failed to scrape {url}: {ex}")
                continue

        return []
    except Exception as e:
        logger.error(f"Error fetching chapters: {e}")
        return []


@mcp.tool()
def get_chapter_content(chapter_url: str) -> str:
    """
    Get text content of a chapter. 
    Dynamically loads config to find specific selectors, or falls back to smart extraction.
    """
    # RELOAD CONFIG every time
    provider = StoryConfigManager.get_provider_for_url(chapter_url)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        response = requests.get(chapter_url, headers=headers, timeout=(3, 15))
        response.raise_for_status()
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        
        # Extract Title immediately for metadata
        page_title = soup.title.get_text(strip=True) if soup.title else ""
        if page_title:
             page_title = re.sub(r"\s*\|\s*.*$", "", page_title)
             page_title = SITE_TITLE_SUFFIX_RE.sub("", page_title)
        
        final_content = ""
        
        if provider:
            logger.info(f"Using provider {provider['name']} for {chapter_url}")
            # Note: soup is already parsed above
            
            content_sel = provider['selectors'].get('content')
            
            if content_sel:
                content_div = soup.select_one(content_sel)
                if content_div:
                    for s in content_div(["script", "div", "iframe", "style"]):
                        s.decompose()
                    final_content = content_div.get_text(separator="\n\n").strip()
            
            if not final_content:
                 logger.warning("Provider selector failed, falling back to heuristic.")
        else:
            logger.info(f"No provider found for {chapter_url}. Using Heuristic Extraction.")
            
        if not final_content:
            # --- Auto-Forward Logic ---
            content, metadata = heuristic_extract(html)
            
            # Check for TOC / Auto-Forward
            if len(content) < 300: 
                # TODO(i18n): VN literals — matches Vietnamese "chapter 1" / "read from start" link text
                first_chap_link = soup.find("a", string=re.compile(r"(chương 1(:|\s)|đọc từ đầu|bắt đầu đọc)", re.I))
                
                if not first_chap_link:
                    chapter_list = soup.find_all("a", href=re.compile(r"chuong-1(/|$)|chapter-1(/|$)", re.I))
                    if chapter_list:
                        first_chap_link = chapter_list[0]
                
                if first_chap_link and first_chap_link.get("href"):
                    next_url = first_chap_link.get("href")
                    if not next_url.startswith("http"):
                        from urllib.parse import urljoin
                        next_url = urljoin(chapter_url, next_url)
                        
                    logger.info(f"Auto-forwarding to Chapter 1: {next_url}")
                    return get_chapter_content(next_url)
            
            # --- AUTO-LEARNING ---
            if metadata and metadata.get("content_selector") and not provider:
                from urllib.parse import urlparse
                domain = urlparse(chapter_url).netloc
                logger.info(f"Auto-learning provider for {domain} with selector: {metadata['content_selector']}")
                
                new_provider = {
                    "domain": domain,
                    "name": domain.split(':')[0],
                    "selectors": {
                        "content": metadata["content_selector"],
                        "title": "h1"
                    },
                    "search_url": None,
                    "trust_level": "auto-learned"
                }
                StoryConfigManager.save_provider(new_provider)
            
            final_content = content

        # [NEW] Save Story to Provider for Prioritization
        if provider and page_title:
             try:
                 # Extract just the Story Name
                 # Format: "Story Name - Chapter X"
                 story_name = page_title
                 if " - " in story_name:
                     story_name = story_name.rsplit(" - ", 1)[0].strip()
                 
                 from urllib.parse import urlparse
                 domain = urlparse(chapter_url).netloc
                 # [FIX] Pass chapter URL (or story URL if achievable, but chapter URL is better than nothing)
                 StoryConfigManager.add_known_story(domain, story_name, story_url_full=chapter_url)
             except Exception as e:
                 logger.error(f"Failed to update known stories: {e}")

        # [FINAL OUTPUT FORMATTING]
        res = f"[[METADATA: {page_title}]]\n{final_content}"
        logger.debug(f"story_server returning: {res[:100]!r}")
        return res

    except Exception as e:
        logger.error(f"Error fetching content: {e}")
        return f"Failed to load chapter: {e}"

@mcp.tool()
def add_story_provider(domain: str, name: str, content_selector: str, title_selector: str = "h1", next_selector: str = None, list_selector: str = None, search_url: str = None) -> str:
    """
    Register a new story provider at runtime.
    Use this when you have successfully analyzed a new site structure.
    """
    provider = {
        "domain": domain,
        "name": name,
        "selectors": {
            "content": content_selector,
            "title": title_selector,
            "next_chapter": next_selector
        },
        "search_url": search_url,
        "list_selector": list_selector,
        "trust_level": "auto-learned"
    }
    StoryConfigManager.save_provider(provider)
    return f"Successfully registered provider: {name} ({domain})"



def _find_best_next_selector(soup: BeautifulSoup) -> list[dict]:
    """
    Find candidate selectors for the 'Next Chapter' link.
    Returns a sorted list of dicts: {'score': int, 'text': str, 'selector': str}
    """
    next_candidates = []
    for a in soup.find_all('a'):
        text = a.get_text(separator=" ", strip=True).lower()
        href = a.get('href', "")
        if not href or len(text) > 50: continue
        
        score = 0
        # TODO(i18n): VN literals — score Vietnamese "next chapter" link text
        if "tiếp" in text: score += 10
        if "next" in text: score += 10
        if "sau" in text: score += 5
        if "chương" in text: score += 2
        
        # Check parent class for "next" or "nav"
        parent = a.parent
        p_cls = " ".join(parent.get("class", [])) if parent else ""
        if "next" in p_cls or "nav" in p_cls: score += 5
        
        if score > 0:
             # Generate selector
             p_tag = parent.name
             p_id = parent.get("id")
             sel = f"{p_tag}"
             if p_id: sel += f"#{p_id}"
             if p_cls: sel += f".{p_cls.replace(' ', '.')}"
             sel += " a"
             
             next_candidates.append({
                 "score": score,
                 "text": text,
                 "selector": sel
             })
    
    next_candidates.sort(key=lambda x: x["score"], reverse=True)
    return next_candidates


# ─────────────────────────────────────────────────────────────────────────────
# AI-DRIVEN CRAWL (redesign — see docs/crawl-redesign-spec.md)
#
# Principle: render the page (JS-aware), hand the LLM a compact, LANGUAGE-AGNOSTIC
# structural summary, let the LLM produce a small "recipe". Code (the worker) then
# enumerates the full chapter list from that recipe — the 1000 URLs never touch
# the LLM context, so token cost is fixed regardless of chapter count.
# ─────────────────────────────────────────────────────────────────────────────

def render_page(url: str, timeout_ms: int = 45000) -> Optional[str]:
    """Fetch a page WITH JS rendering via Scrapling StealthyFetcher.

    Falls back to plain ``requests`` if rendering is unavailable/fails — many
    chapter pages are static HTML (verified: truyenfull chapter content is
    static), so the fallback is fine for content; rendering matters mainly for
    JS-built chapter-list/overview pages (verified: truyencom needs it).

    Returns HTML string, or None on total failure.
    """
    try:
        from scrapling.fetchers import StealthyFetcher
        page = StealthyFetcher.fetch(
            url, headless=True, network_idle=True, timeout=timeout_ms,
        )
        html = getattr(page, "html_content", None)
        if html:
            return html
        # Some Scrapling versions expose body differently
        return str(page) if page is not None else None
    except Exception as e:
        logger.warning(f"[render_page] StealthyFetcher failed ({e}); falling back to requests")
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            resp = requests.get(url, headers=headers, timeout=(3, 20))
            return resp.text if resp.status_code == 200 else None
        except Exception as e2:
            logger.error(f"[render_page] requests fallback also failed: {e2}")
            return None


def _url_path_pattern(path: str) -> str:
    """Collapse digits to '#' so chapter URLs of one story share one pattern.
    Language-agnostic: works on URL structure, not on words.
    e.g. /thien-anh/quyen-1-chuong-12/ -> /thien-anh/quyen-#-chuong-#/"""
    return re.sub(r"\d+", "#", path)


@mcp.tool()
def detect_story_structure(url: str) -> str:
    """Render a story page (overview OR chapter URL) and return a compact,
    language-agnostic STRUCTURAL summary for the LLM to build a crawl recipe.

    The LLM uses the output to decide a recipe:
      { "mode": "list" | "chain",
        "story_root": "/story-slug/",              # same-story guard (path prefix)
        "chapter_link_selector": "...",            # list mode: CSS for chapter <a>
        "chapter_url_pattern": "/slug/...-#...",    # digits→# template the worker matches
        "next_page_selector": "...",               # list mode: pagination link (optional)
        "first_chapter_url": "https://...",         # chain mode: where to start
        "next_chapter_selector": "...",            # chain mode: 'next chapter' link
        "content_selector": "#chapter-c",          # chapter body
        "title_selector": "h1" }

    Output groups same-domain links by URL path-pattern (digits collapsed to #)
    with counts + a sample, so the LLM sees "this pattern appears 53x → likely
    chapter links" vs "this 1x → nav" WITHOUT reading 1000 URLs or any words.
    """
    from urllib.parse import urlparse
    html = render_page(url)
    if not html:
        return json.dumps({"error": f"Could not fetch/render {url}"}, ensure_ascii=False)

    soup = BeautifulSoup(html, "html.parser")
    base = urlparse(url)
    base_host = base.netloc
    segs = [s for s in base.path.split("/") if s]

    # story_root candidates — the same-story guard prefix. NOT always the first
    # path segment: e.g. truyenfull /thien-anh/<ch> → root /thien-anh/, but
    # webnovel /book/<slug>_id/<ch> → root must be /book/<slug>_id/ (just /book/
    # would match EVERY story). We surface multiple candidates and let the LLM
    # pick — language-agnostic, no per-site rule.
    root_candidates = []
    if segs:
        root_candidates.append(f"/{segs[0]}/")                    # segment-1
    # If the CURRENT url is a chapter, the prefix before the chapter segment is
    # a strong story_root candidate (handles /book/<slug>_id/chapter-N).
    cur_path = base.path.rstrip("/")
    m = re.search(r"^(.*?)/(chuong|chapter|chap|ch|episode|quyen|c)[-_/]?\d", cur_path, re.I)
    if m and m.group(1):
        root_candidates.append(m.group(1) + "/")
    if len(segs) >= 2:
        root_candidates.append(f"/{segs[0]}/{segs[1]}/")          # segment-1+2
    # de-dup preserve order
    seen_r = set(); root_candidates = [r for r in root_candidates if not (r in seen_r or seen_r.add(r))]
    story_root = root_candidates[0] if root_candidates else "/"

    # 1. Group same-domain links by path-pattern (structural, language-agnostic)
    import collections
    pat_count = collections.Counter()
    pat_sample = {}
    pat_samples = collections.defaultdict(list)  # up to a few real URLs per pattern
    pat_selector = {}  # representative CSS-ish hint (parent class) per pattern
    for a in soup.find_all("a", href=True):
        p = urlparse(a["href"])
        host = p.netloc or base_host
        if host != base_host:
            continue
        if not p.path or p.path == "/":
            continue
        key = _url_path_pattern(p.path)
        pat_count[key] += 1
        full = a["href"] if a["href"].startswith("http") else urljoin(url, a["href"])
        if len(pat_samples[key]) < 4:
            pat_samples[key].append(full)
        if key not in pat_sample:
            pat_sample[key] = full
            # capture a structural hint: nearest ancestor with class/id
            hint = None
            for anc in [a] + list(a.parents)[:3]:
                if getattr(anc, "get", None):
                    cls = anc.get("class"); aid = anc.get("id")
                    if aid:
                        hint = f"#{aid} a"; break
                    if cls:
                        hint = f".{'.'.join(cls)} a"; break
            pat_selector[key] = hint

    def _common_path_prefix(urls: list) -> str:
        """Longest shared path prefix (segment-wise) of chapter URLs → a precise
        story_root candidate even when site nests under /book/<slug>_id/."""
        paths = [urlparse(u).path for u in urls]
        if not paths:
            return ""
        split = [p.strip("/").split("/") for p in paths]
        pref = []
        for parts in zip(*split):
            if len(set(parts)) == 1:
                pref.append(parts[0])
            else:
                break
        return "/" + "/".join(pref) + "/" if pref else "/"

    # Sort patterns by count desc; the chapter-link pattern usually dominates
    link_patterns = []
    for key, cnt in pat_count.most_common(20):
        same_story = any(key.startswith(_url_path_pattern(r).rstrip("/")) for r in root_candidates)
        # For a high-count pattern, the common prefix of its samples is a great
        # story_root candidate (e.g. /book/<slug>_id/).
        cp = _common_path_prefix(pat_samples[key]) if cnt >= 2 else ""
        if cp and cp not in root_candidates and cp != "/":
            root_candidates.append(cp)
        link_patterns.append({
            "path_pattern": key,
            "count": cnt,
            "sample_url": pat_sample[key],
            "common_prefix": cp,
            "selector_hint": pat_selector.get(key),
            "within_story_root": same_story,
        })

    # 2. Content candidates (density-based; selector only, no language words)
    content_candidates = []
    for tag in soup.find_all(["div", "article", "section", "main"]):
        text = tag.get_text(separator=" ", strip=True)
        if len(text) < 300:
            continue
        p_count = len(tag.find_all("p"))
        sel = None
        if tag.get("id"):
            sel = f"#{tag['id']}"
        elif tag.get("class"):
            sel = f".{'.'.join(tag.get('class'))}"
        content_candidates.append({
            "selector": sel,
            "tag": tag.name,
            "p_count": p_count,
            "text_len": len(text),
            "text_head": text[:120],
        })
    content_candidates.sort(key=lambda c: (c["p_count"], c["text_len"]), reverse=True)
    content_candidates = content_candidates[:5]

    # 3. Pagination / next-page hints (structural attrs only)
    pagination_hints = []
    for a in soup.select("a[rel='next'], .pagination a, .paging a, nav a"):
        href = a.get("href")
        if not href:
            continue
        pagination_hints.append({
            "rel": a.get("rel"),
            "class": a.get("class"),
            "href": href if href.startswith("http") else urljoin(url, href),
            "text_len": len(a.get_text(strip=True)),
        })

    # de-dup root candidates, keep order
    seen_r = set()
    story_root_candidates = [r for r in root_candidates if not (r in seen_r or seen_r.add(r))]

    out = {
        "url": url,
        "host": base_host,
        "story_root_candidates": story_root_candidates,
        "story_root_guess": story_root,
        "rendered": True,
        "link_patterns": link_patterns,
        "content_candidates": content_candidates,
        "pagination_hints": pagination_hints[:8],
        "guidance": (
            "Build a recipe (language-agnostic — use URL/DOM structure, never words). "
            "STORY_ROOT: pick from story_root_candidates the prefix that uniquely "
            "identifies THIS story. WARNING: the first candidate (first path segment) "
            "is often TOO BROAD — e.g. '/book/' matches every story on the site. Prefer "
            "the 'common_prefix' of the high-count chapter link_pattern (e.g. "
            "'/book/<slug>_<id>/') which is unique to this story. "
            "MODE: if a within_story_root link_pattern has a high count (chapters listed "
            "here) → mode='list' with its selector_hint + a chapter sample as "
            "chapter_url_pattern + a pagination_hint as next_page_selector. If the page "
            "is a single chapter (no high-count chapter pattern; a 'next' link exists) → "
            "mode='chain' with first_chapter_url + next_chapter_selector. "
            "CONTENT: pick content_selector from content_candidates (highest p_count, "
            "text_head reads like narrative). Then call verify_chapters before crawling."
        ),
    }
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def get_story_page_structure(url: str) -> str:
    """
    Analyzes a chapter URL and returns a simplified schematic of the content candidates.
    Use this to help determine the correct 'content_selector' and 'title_selector'.
    
    Returns: A string listing potential container Divs/Articles with their classes, ids, properties and a score.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=(3, 15))
        soup = BeautifulSoup(resp.text, "html.parser")

        # 1. Clean DOM
        _clean_dom(soup)
        
        # 2. Score Candidates
        candidates = []
        for tag in soup.find_all(['div', 'article', 'section']):
             # Re-use the density scoring logic briefly here or abstract it?
             # For now, duplicate simplified logic to keep it robust purely for display
             
             p_count = len(tag.find_all('p')) 
             text = tag.get_text(separator=" ", strip=True)
             text_len = len(text)
             if text_len < 100: continue
             
             score = (p_count * 50) + (text_len / 20)
             
             tag_name = tag.name
             t_id = tag.get("id")
             t_cls = tag.get("class")
             
             selector_part = tag_name
             if t_id: selector_part += f"#{t_id}"
             if t_cls: selector_part += f".{'.'.join(t_cls)}"
             
             preview = text[:500].replace("\n", " ")
             
             candidates.append({
                 "score": int(score),
                 "selector": selector_part,
                 "p_count": p_count,
                 "length": text_len,
                 "preview": preview
             })
             
        # [NEW] 3. Score Next Chapter Candidates
        next_candidates = _find_best_next_selector(soup)

        output = [f"Analysis for {url}:\n"]
        output.append("TOP 3 CONTENT CANDIDATES:")
        for c in candidates[:3]:
            output.append(f"- Score: {c['score']} | {c['selector']}")
            output.append(f"  P: {c['p_count']}, Len: {c['length']}")
        
        output.append("\nTOP NEXT LINK CANDIDATES:")
        for c in next_candidates[:3]:
             output.append(f"- Score: {c['score']} | Text: '{c['text']}' | Selector: {c['selector']}")
             output.append("")
            
        return "\n".join(output)
        
    except Exception as e:
        return f"Error analyzing page: {e}"

@mcp.tool()
def analyze_story_pattern(url: str) -> str:
    """
    Analyze a web page to detect story content and title selectors.
    Returns suggested selectors as a JSON string.
    """
    return _analyze_story_pattern_impl(url)

def _analyze_story_pattern_impl(url: str) -> str:
    try:
        # [NEW] Check Known Providers First
        provider = StoryConfigManager.get_provider_for_url(url)
        if provider and provider.get('selectors'):
            logger.info(f"Analyzer found known provider for {url}: {provider['name']}")
            selectors = provider['selectors']
            # Ensure keys exist
            if "content" not in selectors: selectors["content"] = "div.content"
            if "title" not in selectors: selectors["title"] = "h1"
            if "next_chapter" not in selectors: selectors["next_chapter"] = None
            return json.dumps(selectors, ensure_ascii=False)

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=(3, 15))
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # Heuristic Content Detection
        content, metadata = heuristic_extract(resp.text)
        
        selectors = {
            "content": metadata.get("content_selector") if metadata else None,
            "title": "h1", # Default
            "next_chapter": None
        }
        
        # Try to find Next Chapter button
        # Try to find Next Chapter button using robust logic
        next_candidates = _find_best_next_selector(soup)
        next_chapter_sel = next_candidates[0]['selector'] if next_candidates else None
        
        result = {
            "content": metadata.get("content_selector") if metadata else None,
            "title": "h1", 
            "next_chapter": next_chapter_sel,
            "trust_level": "auto_learn"  # Default for new analysis
        }
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        return json.dumps({"error": str(e)})

@mcp.tool()
def test_crawl_chapter(url: str, content_selector: str, title_selector: str = "h1") -> str:
    """
    Test crawling a single chapter with provided selectors.
    Returns: Title, First 200 chars, Last 200 chars.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=(3, 15))
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Extract Title
        title_tag = soup.select_one(title_selector)
        title = title_tag.get_text(strip=True) if title_tag else soup.title.get_text(strip=True)
        
        # Extract Content
        content_div = soup.select_one(content_selector)
        if not content_div:
            return "Error: Content selector did not match any element."
            
        # Clean
        for s in content_div(["script", "style", "div", "iframe", "noscript"]):
             # Be careful not to delete p tags inside div if extraction is broad
             if s.name == 'div' and len(s.find_all('p')) > 0: continue
             s.decompose()
             
        text = content_div.get_text(separator="\n\n").strip()
        
        preview = f"SUCCESS: Found {len(text)} chars.\nTITLE: {title}\nSELECTOR: {content_selector}\nSTART: {text[:500]}...\n\nEND: ...{text[-500:]}"
        return preview
    except Exception as e:
        return f"Test failed: {e}"


def _recipe_first_chapters(recipe: dict, n: int = 3) -> list[str]:
    """Resolve the first N chapter URLs from a recipe (LIST: enumerate page 1;
    CHAIN: follow next_chapter_selector from first_chapter_url). Used by
    verify_chapters — only N pages fetched, so this is cheap."""
    from urllib.parse import urlparse
    mode = recipe.get("mode", "list")
    story_root = recipe.get("story_root") or "/"
    if mode == "list":
        # Reuse the poller's enumerator for parity with the real crawl.
        try:
            from services.crawl_poller import CrawlPoller
            chs = CrawlPoller()._enumerate_list_mode("verify", recipe.get("overview_url") or recipe.get("first_chapter_url") or "", recipe)
            return [c["url"] for c in chs[:n]]
        except Exception as e:
            logger.warning(f"[verify] list enumerate failed: {e}")
            return []
    # CHAIN: walk next links
    urls = []
    cur = recipe.get("first_chapter_url")
    nsel = recipe.get("next_chapter_selector")
    base_host = urlparse(cur).netloc if cur else ""
    for _ in range(n):
        if not cur:
            break
        urls.append(cur)
        html = render_page(cur)
        if not html or not nsel:
            break
        nx = BeautifulSoup(html, "html.parser").select_one(nsel)
        nxt = urljoin(cur, nx["href"]) if (nx and nx.get("href")) else None
        # same-story guard
        if not nxt or urlparse(nxt).netloc not in ("", base_host) or not urlparse(nxt).path.startswith(story_root):
            break
        cur = nxt
    return urls


@mcp.tool()
def verify_chapters(recipe_json: str) -> str:
    """Verify a crawl recipe on the FIRST 3 CHAPTERS before a full crawl.

    Stronger than testing one chapter: fetches 3 consecutive chapters, returns
    a longer preview of each, and runs language-agnostic CONTINUITY checks so
    the agent can catch a wrong/garbage selector OR a drift into another story
    BEFORE committing to 1000 chapters.

    Pass the recipe as JSON (from detect_story_structure analysis). Returns a
    verdict {ok, chapters:[{url,len,head,tail}], issues:[...]} for the agent to
    decide: crawl, or fix the recipe and re-verify.
    """
    try:
        recipe = json.loads(recipe_json) if isinstance(recipe_json, str) else recipe_json
    except Exception as e:
        return json.dumps({"ok": False, "issues": [f"recipe_json parse error: {e}"]}, ensure_ascii=False)

    content_sel = recipe.get("content_selector")
    title_sel = recipe.get("title_selector", "h1")
    if not content_sel:
        return json.dumps({"ok": False, "issues": ["recipe missing content_selector"]}, ensure_ascii=False)

    urls = _recipe_first_chapters(recipe, n=3)
    if not urls:
        return json.dumps({"ok": False, "issues": ["could not resolve first chapters from recipe (mode/selectors likely wrong)"]}, ensure_ascii=False)

    chapters = []
    issues = []
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    for i, u in enumerate(urls):
        html = None
        try:
            r = requests.get(u, headers=headers, timeout=(3, 15))
            html = r.text if r.status_code == 200 and len(r.text) > 2000 else None
        except Exception:
            pass
        if not html:
            html = render_page(u)
        if not html:
            issues.append(f"ch{i+1}: could not fetch {u}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        cdiv = soup.select_one(content_sel)
        text = ""
        if cdiv:
            for s in cdiv(["script", "style", "iframe"]):
                s.decompose()
            text = cdiv.get_text(separator="\n\n").strip()
        t = soup.select_one(title_sel)
        chapters.append({
            "url": u,
            "title": t.get_text(strip=True) if t else None,
            "len": len(text),
            "head": text[:1000],
            "tail": text[-500:] if len(text) > 500 else "",
        })

    # ── Continuity / sanity checks (language-agnostic) ──
    if len(chapters) < 3:
        issues.append(f"only resolved {len(chapters)}/3 chapters")
    for c in chapters:
        if c["len"] < 500:
            issues.append(f"{c['url']}: content too short ({c['len']} chars) — selector likely wrong/garbage")
    # distinct content: 3 chapters must not be identical (selector grabbing a static block)
    heads = [c["head"] for c in chapters if c["head"]]
    if len(heads) >= 2 and len(set(heads)) == 1:
        issues.append("all chapters returned identical content — selector grabs a non-chapter block")
    # URL monotonic (numbers increase) — drift / wrong order guard
    nums = []
    from urllib.parse import urlparse
    for c in chapters:
        ns = [int(x) for x in re.findall(r"\d+", urlparse(c["url"]).path)]
        nums.append(ns)
    if len(nums) >= 2 and not all(nums[i] <= nums[i+1] for i in range(len(nums)-1)):
        issues.append("chapter URLs not in increasing order — list/sort may be wrong")

    return json.dumps({
        "ok": len(issues) == 0,
        "chapter_count_checked": len(chapters),
        "chapters": chapters,
        "issues": issues,
        "guidance": "If ok=false, inspect issues. Common fix: choose a different content_selector "
                    "(longer narrative text, not nav/sidebar) and re-run verify_chapters. "
                    "If chapters are short or identical, the selector is wrong.",
    }, ensure_ascii=False)



@mcp.tool()
def crawl_story(recipe_json: str, max_chapters: int = 2000, speed: float = 2.0) -> str:
    """
    Start a background crawl job from a VERIFIED recipe (preferred path).

    Call this AFTER detect_story_structure + verify_chapters confirm the recipe
    is good. The CrawlPoller (FastAPI process) enumerates the full chapter list
    from the recipe (the 1000 URLs never pass through the LLM) and fetches each
    chapter — so token cost is independent of chapter count.

    Args:
        recipe_json: JSON recipe. Required keys depend on mode:
          { "mode": "list" | "chain",
            "story_root": "/story-slug/",         # same-story guard (REQUIRED)
            "content_selector": "#chapter-c",      # chapter body (REQUIRED)
            "title_selector": "h1",
            # list mode:
            "overview_url": "https://.../story/",  # page that lists chapters
            "chapter_url_pattern": "/story/chuong-1/",  # a sample chapter URL (digits→# template)
            "next_page_selector": "...",           # optional pagination link
            # chain mode:
            "first_chapter_url": "https://.../chuong-1/",
            "next_chapter_selector": "a.next",
            "story_title": "..."                    # optional; else derived from page
          }
        max_chapters: safety cap.
        speed: delay between chapter fetches (seconds).
    """
    try:
        recipe = json.loads(recipe_json) if isinstance(recipe_json, str) else recipe_json
    except Exception as e:
        return f"Error: recipe_json is not valid JSON: {e}"
    if not recipe.get("content_selector"):
        return "Error: recipe missing required 'content_selector'."
    if not recipe.get("story_root"):
        return "Error: recipe missing required 'story_root' (same-story guard)."
    start_url = (recipe.get("overview_url") or recipe.get("first_chapter_url")
                 or recipe.get("chapter_url_pattern"))
    if not start_url:
        return "Error: recipe needs overview_url (list mode) or first_chapter_url (chain mode)."

    job_id = str(uuid.uuid4())
    params = json.dumps({
        "recipe": recipe,
        "speed": speed,
        "max_chapters": max_chapters,
    }, ensure_ascii=False)
    try:
        from core.database import get_db_session, CrawlJob, init_db
        init_db()
        db = get_db_session()
        db.add(CrawlJob(job_id=job_id, status="pending", start_url=start_url, params=params))
        db.commit()
        db.close()
    except Exception as e:
        logger.error(f"Failed to create crawl job: {e}")
        return f"Error creating crawl job: {e}"

    return (f"Started crawl job {job_id}. The crawl runs in the background. "
            f"Report this job_id to the user with the tag [[[CRAWL_STARTED: {job_id}]]]. "
            f"Do NOT poll get_crawl_status repeatedly.")


@mcp.tool()
def resume_crawl(job_id: str, recipe_patch_json: str = "{}", mark_done: bool = False) -> str:
    """Resume a crawl job that flagged 'needs_attention' (self-heal path).

    The crawl worker pauses and wakes CrawlStoriesAgent when it gets stuck
    (selector broke mid-crawl, drift, etc). After diagnosing (detect_story_
    structure + verify_chapters), call this to fix the recipe and continue
    FROM THE CHECKPOINT (not from chapter 1).

    Args:
        job_id: the stuck job.
        recipe_patch_json: JSON of recipe keys to override (e.g.
            {"content_selector": ".reading-content"}). Merged onto the saved recipe.
        mark_done: pass true if the "stuck" point is actually the real end of
            the story (no fix needed) — marks the job completed as-is.
    """
    try:
        from core.database import get_db_session, CrawlJob
        db = get_db_session()
        job = db.query(CrawlJob).filter(CrawlJob.job_id == job_id).first()
        if not job:
            db.close()
            return json.dumps({"status": "not_found"})
        params = json.loads(job.params) if job.params else {}
        if mark_done:
            job.status = "completed"
            job.message = f"Marked done by agent at chapter {job.current_chapter}."
            db.commit(); db.close()
            return json.dumps({"status": "completed", "job_id": job_id})
        try:
            patch = json.loads(recipe_patch_json) if isinstance(recipe_patch_json, str) else (recipe_patch_json or {})
        except Exception as e:
            db.close()
            return json.dumps({"status": "error", "message": f"recipe_patch_json invalid: {e}"})
        recipe = params.get("recipe") or {}
        recipe.update(patch)
        params["recipe"] = recipe
        # resume_from: checkpoint stuck_index → worker skips already-saved chapters
        cp = params.get("checkpoint") or {}
        params["resume_from"] = cp.get("stuck_index", 0)
        job.params = json.dumps(params, ensure_ascii=False)
        job.status = "pending"   # CrawlPoller re-picks it up
        job.message = "Resuming after recipe fix..."
        db.commit(); db.close()
        return json.dumps({"status": "resuming", "job_id": job_id,
                           "resume_from_chapter": params["resume_from"] + 1,
                           "patched_keys": list(patch.keys())}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def get_crawl_status(job_id: str) -> str:
    """Get the status of a crawl job. IMPORTANT: If status is 'running', report current progress to user and STOP. Do NOT poll repeatedly."""
    try:
        from core.database import get_db_session, CrawlJob
        db = get_db_session()
        job = db.query(CrawlJob).filter(CrawlJob.job_id == job_id).first()
        if not job:
            db.close()
            return json.dumps({"status": "not_found"})
        result = {
            "job_id": job.job_id,
            "status": job.status,
            "story_title": job.story_title,
            "current": job.current_chapter,
            "total": job.total_chapters,
            "message": job.message,
        }
        db.close()
        if result["status"] == "running":
            result["_instruction"] = "Job is running in background. Report this status to the user and STOP. Do NOT call get_crawl_status again."
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


def delete_local_story(story_id: str) -> dict:
    """Delete a story and all its chapters from local storage."""
    story_dir = os.path.join("data/stories", story_id)
    if os.path.exists(story_dir):
        import shutil
        shutil.rmtree(story_dir)
        return {"status": "deleted", "id": story_id}
    return {"status": "not_found", "message": "Story not found"}

@mcp.tool()
def get_local_stories() -> str:
    """List all stories downloaded locally as text chapters.
    Returns JSON list with title, chapter count, and path for each story."""
    stories_dir = os.path.join(DATA_DIR, "stories")
    if not os.path.exists(stories_dir):
        return "[]"
        
    results = []
    # os.listdir can return files too, filter dirs
    try:
        for name in os.listdir(stories_dir):
            path = os.path.join(stories_dir, name)
            if os.path.isdir(path):
                # Count files
                chapter_files = [f for f in os.listdir(path) if f.endswith(".txt")]
                count = len(chapter_files)
                results.append({"title": name, "chapters": count, "path": path})
    except Exception as e:
        logger.error(f"Error listing local stories: {e}")
            
    return json.dumps(results, ensure_ascii=False)

@mcp.tool()
def get_local_story_chapters(story_title: str) -> str:
    """List chapter files for a local story. Use the exact title from get_local_stories().
    Returns sorted JSON array of filenames (e.g. 001_Title.txt, 002_Title.txt)."""
    try:
        # Search by directory name (exact match or close enough)
        stories_dir = os.path.join(DATA_DIR, "stories")
        target_dir = os.path.join(stories_dir, story_title)
        
        # Security check
        if not os.path.abspath(target_dir).startswith(os.path.abspath(stories_dir)):
             return "[]"

        if not os.path.exists(target_dir):
            return "[]"
            
        files = [f for f in os.listdir(target_dir) if f.endswith(".txt")]
        files.sort() # Sort by filename (001_..., 002_...)
        
        return json.dumps(files, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error listing chapters: {e}")
        return "[]"

@mcp.tool()
def get_local_chapter_content(story_title: str, chapter_filename: str) -> str:
    """Get content of a local chapter."""
    try:
        stories_dir = os.path.join(DATA_DIR, "stories")
        file_path = os.path.join(stories_dir, story_title, chapter_filename)
        
        # Security check
        if not os.path.abspath(file_path).startswith(os.path.abspath(stories_dir)):
             return "Error: Invalid path"

        if not os.path.exists(file_path):
            return "Error: File not found"
            
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
            
    except Exception as e:
        return f"Error reading file: {e}"

# --- Smart Story Finder ---

def _normalize_text(text: str) -> str:
    """Normalize text for matching: lowercase, strip whitespace."""
    return text.lower().strip()

def _strip_diacritics(text: str) -> str:
    """Remove Vietnamese diacritics for broader matching."""
    nfkd = unicodedata.normalize('NFD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()

def _fuzzy_match(query: str, target: str) -> bool:
    """Check if query matches target using normalized and diacritics-stripped comparison."""
    q_norm = _normalize_text(query)
    t_norm = _normalize_text(target)

    if q_norm in t_norm or t_norm in q_norm:
        return True

    q_stripped = _strip_diacritics(query)
    t_stripped = _strip_diacritics(target)
    if q_stripped in t_stripped or t_stripped in q_stripped:
        return True

    return False



def _find_chapter_in_list(chapters: list, chapter_number: int) -> Optional[str]:
    """Find a specific chapter URL from a chapter list by chapter number."""
    if not chapters:
        return None

    for ch in chapters:
        title = ch.get("title", "")
        url = ch.get("url", "")

        # Match "Chương 10" or "Chapter 10" in title
        # TODO(i18n): VN literals — regex matches Vietnamese chapter titles
        if re.search(rf'(chương|chapter|hồi)\s+0*{chapter_number}(\s|$|:|\.|\,)', title, re.I):
            return url

        # Match in URL: chuong-10/, chapter-10/
        if re.search(rf'(chuong|chapter|hoi)[-_]0*{chapter_number}(/|$)', url, re.I):
            return url

    return None


def _save_pending_read(action: dict):
    """Write pending read action to DB for FastAPI process to pick up.
    Replaces file-based pending_read.json — atomic, crash-safe."""
    try:
        from core.database import PendingAction
        db = _get_db()
        try:
            # Clear any stale pending actions (older than 60s)
            cutoff = time.time() - 60
            db.query(PendingAction).filter(PendingAction.created_at < cutoff).delete()
            
            # Insert new action
            entry = PendingAction(
                action_type=action.get("type", "UNKNOWN"),
                payload_json=json.dumps(action, ensure_ascii=False),
            )
            db.add(entry)
            db.commit()
            logger.info(f"Saved pending read action to DB: {action.get('type')}")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Failed to save pending read: {e}")

@mcp.tool()
def find_story_chapter(story_name: str, chapter_number: int) -> str:
    """Find a story chapter from all available sources.
    Searches in priority order: local stories, known web stories, web search.
    Returns the result as a ready-to-use response message.
    """
    searched = []

    # --- Priority 1: Local Stories (text files in data/stories/) ---
    try:
        searched.append("local")
        stories_dir = os.path.join(DATA_DIR, "stories")
        if os.path.exists(stories_dir):
            for dir_name in os.listdir(stories_dir):
                dir_path = os.path.join(stories_dir, dir_name)
                if not os.path.isdir(dir_path):
                    continue

                if _fuzzy_match(story_name, dir_name):
                    chapter_prefix = f"{chapter_number:03d}_"
                    files = [f for f in os.listdir(dir_path) if f.endswith(".txt") and f.startswith(chapter_prefix)]

                    if files:
                        filename = files[0]
                        logger.info(f"find_story_chapter: Found local: {dir_name}/{filename}")
                        _save_pending_read({"type": "READ_LOCAL", "story_title": dir_name, "chapter_filename": filename})
                        # ONE canonical tag for local playback: [[[READ_LOCAL: title|file]]].
                        # The chat/voice routes resolve playback via the pending-read queue
                        # above (check_pending_read → handle_read_local). The old extra
                        # [[[AUDIO_URL: ...]]] tag was redundant — same mechanism, two
                        # surface strings — and leaked into the chat bubble. Dropped.
                        response_text = (
                            f"Now playing {dir_name} chapter {chapter_number}. "
                            f"[[[READ_LOCAL: {dir_name}|{filename}]]]"
                        )
                        return json.dumps({
                            "response": response_text,
                            "source": "local"
                        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"find_story_chapter: local stories error: {e}")

    # --- Priority 3: Known Stories (previously accessed web stories) ---
    try:
        searched.append("known_stories")
        providers = StoryConfigManager.load_providers()
        matched_story_url = None

        for p in providers:
            for known in p.get("known_stories", []):
                title = known.get("title", "") if isinstance(known, dict) else str(known)
                url = known.get("url", "") if isinstance(known, dict) else ""

                if title and _fuzzy_match(story_name, title) and url and url.startswith("http"):
                    matched_story_url = url
                    break
            if matched_story_url:
                break

        if matched_story_url:
            chapters = _get_story_chapters_impl(matched_story_url)
            chapter_url = _find_chapter_in_list(chapters, chapter_number)
            if chapter_url:
                logger.info(f"find_story_chapter: Found in known stories: {chapter_url}")
                _save_pending_read({"type": "READ_STORY", "url": chapter_url})
                return json.dumps({
                    "response": f"Now playing {story_name} chapter {chapter_number}.",
                    "source": "known_stories"
                }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"find_story_chapter: known stories error: {e}")

    # --- Priority 4: Web Search (last resort) ---
    try:
        searched.append("web_search")
        search_results = json.loads(search_stories(story_name))

        for sr in search_results:
            story_url = sr.get("url", "")
            if not story_url:
                continue

            chapters = _get_story_chapters_impl(story_url)
            chapter_url = _find_chapter_in_list(chapters, chapter_number)
            if chapter_url:
                found_title = sr.get("title", story_name)
                logger.info(f"find_story_chapter: Found via web search: {chapter_url}")
                _save_pending_read({"type": "READ_STORY", "url": chapter_url})
                return json.dumps({
                    "response": f"Now playing {found_title} chapter {chapter_number}.",
                    "source": "web_search"
                }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"find_story_chapter: web search error: {e}")

    # --- Not Found ---
    logger.warning(f"find_story_chapter: Not found: '{story_name}' chapter {chapter_number}. Searched: {searched}")
    return json.dumps({
        "response": f"Could not find '{story_name}' chapter {chapter_number}.",
        "error": True,
        "searched": searched
    }, ensure_ascii=False)

if __name__ == "__main__":
    mcp.run()
