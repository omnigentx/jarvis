import logging
import requests
import json
import os
import re
import unicodedata
from pathlib import Path
from bs4 import BeautifulSoup

from helpers.http_safety import get_capped_text
from helpers.path_safety import safe_story_path
from mcp.server.fastmcp import FastMCP
from typing import List, Dict, Optional
import time
from urllib.parse import urljoin, quote
import uuid
import threading
from dotenv import load_dotenv

# Logging — inherits config from centralized logging_config
logger = logging.getLogger("story_server")

# Constants
# DATA_DIR: absolute path to backend/data/ — works in MCP subprocess context
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# Load .env from backend directory
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)
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
                        
                        # Heuristic: Link text must look like a chapter
                        # TODO(i18n): VN literals — regex matches Vietnamese chapter link text/URLs
                        if re.search(r'(chương|chapter|hồi)\s+\d+', text, re.I) or re.search(r'chapter|chuong', href, re.I):
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
                        # TODO(i18n): VN literals — matches Vietnamese pagination link text
                        next_link = soup.find("a", string=re.compile(r"(Trang tiếp|Tiếp|Next|Sau|»|›)", re.I))

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
def test_crawl_chapter(chapter_url: str, content_selector: str = None, title_selector: str = "h1") -> str:
    """
    Test crawling a single chapter to verify selectors.
    If selectors are provided, uses them. Otherwise checks saved config or heuristics.
    Returns preview of Title + Start/End content.
    """
    logger.info(f"Testing crawl for: {chapter_url} with sel={content_selector}")
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"}
        resp = requests.get(chapter_url, headers=headers, timeout=(3, 15))
        resp.raise_for_status()
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Determine Selectors
        c_sel = content_selector
        t_sel = title_selector
        
        # If not provided, try to find from config
        if not c_sel:
             provider = StoryConfigManager.get_provider_for_url(chapter_url)
             if provider and provider.get('selectors'):
                 c_sel = provider['selectors'].get('content')
                 t_sel = provider['selectors'].get('title', 'h1')
        
        # Extraction
        title_text = "Unknown Title"
        if t_sel:
            t_tag = soup.select_one(t_sel)
            if t_tag: title_text = t_tag.get_text(strip=True)
            
        content_text = ""
        if c_sel:
            c_tag = soup.select_one(c_sel)
            if c_tag:
                # Clean before extraction
                _clean_dom(c_tag) 
                content_text = c_tag.get_text(separator="\n", strip=True)
        else:
            # Fallback to heuristic
            content_text, _ = heuristic_extract(resp.text)
            title_text = "Heuristic Title"

        if not content_text:
            return f"Error: No content found with selector '{c_sel}'"
            
        len_txt = len(content_text)
        preview_start = content_text[:200].replace("\n", " ")
        preview_end = content_text[-200:].replace("\n", " ")
        
        return (f"SUCCESS: Found {len_txt} chars.\n"
                f"TITLE: {title_text}\n"
                f"SELECTOR: {c_sel}\n"
                f"START: {preview_start}\n"
                f"...\n"
                f"END: {preview_end}")
                
    except Exception as e:
        return f"Error testing crawl: {e}"   

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
             # TODO(i18n): VN literal — strips trailing "- Truyện..." (Vietnamese "Story") suffix from page titles
             page_title = re.sub(r"\s*-\s*Truyện.*$", "", page_title, flags=re.I)
        
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

# Global Crawl State — DB-backed for cross-process visibility
_crawl_db_initialized = False

def _save_crawl_status():
    """Persist crawl_jobs to DB so FastAPI can read it."""
    global _crawl_db_initialized
    try:
        from core.database import get_db_session, CrawlJob, init_db
        if not _crawl_db_initialized:
            init_db()
            _crawl_db_initialized = True
        from datetime import datetime
        db = get_db_session()
        try:
            for job_id, job in crawl_jobs.items():
                existing = db.query(CrawlJob).filter(CrawlJob.job_id == job_id).first()
                if existing:
                    existing.status = job.get("status", "unknown")
                    existing.story_title = job.get("story_title")
                    existing.current_chapter = job.get("current", 0)
                    existing.total_chapters = job.get("total", 0)
                    existing.message = job.get("message")
                    existing.updated_at = datetime.now().timestamp()
                else:
                    db.add(CrawlJob(
                        job_id=job_id,
                        status=job.get("status", "pending"),
                        story_title=job.get("story_title"),
                        current_chapter=job.get("current", 0),
                        total_chapters=job.get("total", 0),
                        message=job.get("message"),
                        start_url=job.get("start_url"),
                    ))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Failed to save crawl status to DB: {e}")

crawl_jobs = {}



@mcp.tool()
def crawl_story(url: str, content_selector: str = None, title_selector: str = "h1", next_selector: str = None, speed: float = 2.0, max_chapters: int = 2000) -> str:
    """
    Start a background job to crawl a story.
    The job is inserted as 'pending' into DB. FastAPI CrawlPoller picks it up.
    
    Args:
        url: The starting chapter URL.
        content_selector: CSS selector for chapter text (Required if not saved).
        title_selector: CSS selector for chapter title.
        next_selector: CSS selector for 'next chapter' link.
        speed: Delay between requests (seconds).
        max_chapters: Limit number of chapters.
    """
    job_id = str(uuid.uuid4())
    params = json.dumps({
        "content_selector": content_selector,
        "title_selector": title_selector,
        "next_selector": next_selector,
        "speed": speed,
        "max_chapters": max_chapters,
    })
    try:
        from core.database import get_db_session, CrawlJob, init_db
        init_db()
        db = get_db_session()
        db.add(CrawlJob(
            job_id=job_id,
            status="pending",
            start_url=url,
            params=params,
        ))
        db.commit()
        db.close()
    except Exception as e:
        logger.error(f"Failed to create crawl job: {e}")
        return f"Error creating crawl job: {e}"

    return f"Started crawl job {job_id}. Report this job_id to the user. The crawl runs in the background. Do NOT poll get_crawl_status repeatedly."

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

def _crawl_worker(job_id: str, start_url: str, content_selector: str, title_selector: str, next_selector: str, speed: float, max_chapters: int):
    import traceback as _tb
    _log_path = os.path.join(DATA_DIR, "crawl_debug.log")
    def _dbg(msg):
        line = f"[{time.strftime('%H:%M:%S')}][{job_id[:8]}] {msg}"
        print(line, flush=True)
        try:
            with open(_log_path, "a") as _f:
                _f.write(line + "\n")
        except: pass
    _dbg(f"Worker started: {start_url}")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        # 1. Check Cancellation Early
        if crawl_jobs[job_id].get("status") == "cancelled":
             logger.info(f"[{job_id}] Cancelled before start.")
             return

        # 2. Determine Story Name & Chapter 1
        logger.info(f"[{job_id}] Initializing crawl for: {start_url}")

        # [NEW] Check for Saved Provider Overrides OR Auto-Discover
        provider = StoryConfigManager.get_provider_for_url(start_url)
        
        if not provider:
            logger.info(f"[{job_id}] No known provider for {start_url}. Initiating Auto-Discovery...")
            try:
                # 1. Analyze
                analysis_json = _analyze_story_pattern_impl(start_url)
                analysis = json.loads(analysis_json)
                
                if analysis and analysis.get("content"):
                    # 2. Extract Domain
                    from urllib.parse import urlparse
                    domain_obj = urlparse(start_url)
                    domain = domain_obj.netloc
                    name = domain.split(':')[0]
                    
                    # 3. Create Provider
                    new_provider = {
                        "domain": domain,
                        "name": name.capitalize(),
                        "selectors": {
                            "content": analysis.get("content"),
                            "title": analysis.get("title", "h1"),
                            "next_chapter": analysis.get("next_chapter")
                        },
                        "trust_level": "auto-learned"
                    }
                    
                    # 4. Save and Use
                    StoryConfigManager.save_provider(new_provider)
                    logger.info(f"[{job_id}] Auto-Discovery Successful! Registered provider: {name} ({domain})")
                    provider = new_provider
                else:
                    logger.warning(f"[{job_id}] Auto-Discovery failed to find reliable selectors.")
            except Exception as e:
                logger.error(f"[{job_id}] Auto-Discovery failed: {e}")

        if provider:
            logger.info(f"[{job_id}] Found provider: {provider.get('name')} ({provider.get('trust_level')})")
            
            # [NEW] Pipeline Step 2: Verification
            if provider.get("trust_level") != "verified":
                logger.info(f"[{job_id}] Verifying provider '{provider.get('name')}'...")
                
                # Use current selectors
                sels = provider.get('selectors', {})
                c_sel = sels.get('content')
                t_sel = sels.get('title', 'h1')
                
                if not c_sel:
                     logger.warning(f"[{job_id}] Verification failed: No content selector.")
                     provider = None # Abort use
                else:
                    # Test extraction
                    test_result = test_crawl_chapter(start_url, c_sel, t_sel)
                    
                    # Analyze test result (simple check: length > 200 and no error)
                    if "Error:" not in test_result and len(test_result.split("START:\n")[-1]) > 200:
                         logger.info(f"[{job_id}] Verification PASSED. Promoting to 'verified'.")
                         provider["trust_level"] = "verified"
                         StoryConfigManager.save_provider(provider)
                    else:
                         logger.warning(f"[{job_id}] Verification FAILED. Result: {test_result[:100]}...")
                         # Do not use this provider, fallback to heuristic or fail
                         provider = None
            
            if provider:
                logger.info(f"[{job_id}] Using verified provider: {provider.get('name')}")
                if provider.get('selectors'):
                    sels = provider['selectors']
                    if sels.get('content'): 
                        content_selector = sels['content']
                    if sels.get('title'): 
                        title_selector = sels['title']
                    if sels.get('next_chapter'): 
                        next_selector = sels['next_chapter']
        
        # [AUTO-DETECT CHAPTER 1]
        try:
             # Heuristic: If URL looks like a chapter, check if we can find Chapter 1
             domain_match = re.search(r"https?://([^/]+)", start_url)
             if domain_match:
                 logger.info(f"[{job_id}] checking if we should rewind to Chapter 1...")
                 # Call implementation directly (bypassing Tool wrapper)
                 chapters = _get_story_chapters_impl(start_url)
                 logger.info(f"[{job_id}] Found {len(chapters)} chapters from list.")
                 
                 if chapters and len(chapters) > 0:
                     # Filter for "Chapter 1"
                     # TODO(i18n): VN literals — regex matches Vietnamese chapter-1/prologue titles
                     c1 = next((c for c in chapters if re.search(r'(chương|chapter)\s+0*1(\s+:|$|\D)', c['title'], re.I) or re.search(r'(mở đầu|văn án)', c['title'], re.I)), None)
                     
                     if c1:
                         if c1['url'] != start_url:
                             logger.info(f"[{job_id}] Switching start URL to Found Chapter 1: {c1['url']}")
                             start_url = c1['url']
                     if c1:
                         if c1['url'] != start_url:
                             logger.info(f"[{job_id}] Switching start URL to Found Chapter 1: {c1['url']}")
                             start_url = c1['url']
                     else:
                         # Fallback: If no explicit "Chapter 1" found, but list exists and has items
                         # And start_url seems to be a Story URL (not in list)
                         logger.info(f"[{job_id}] 'Chapter 1' not explicitly detected by regex. Defaulting to first chapter in list.")
                         c1 = chapters[0]
                         if c1['url'] != start_url:
                             logger.info(f"[{job_id}] Switching start URL to First Chapter: {c1['url']}")
                             start_url = c1['url']
        except Exception as e:
            logger.warning(f"[{job_id}] Chapter 1 auto-detect error: {e}")
            chapters = [] # Retrieve failed, reset to empty for safety

        # Check Cancellation Again
        if crawl_jobs[job_id].get("status") == "cancelled":
             logger.info(f"[{job_id}] Cancelled during setup.")
             return

        resp = requests.get(start_url, headers=headers, timeout=(3, 15))
        soup = BeautifulSoup(resp.text, "html.parser")
        title_tag = soup.select_one(title_selector)
        full_title = title_tag.get_text(strip=True) if title_tag else "Unknown Story"
        
        story_name = full_title.split("-")[0].strip()
        # Cleanup if title captured chapter info
        # TODO(i18n): VN literal — strips Vietnamese chapter suffix from scraped page titles
        story_name = re.sub(r'\s*(Chương|Chapter)\s+\d+.*$', '', story_name, flags=re.I).strip()
        
        if not story_name: story_name = f"Story_{int(time.time())}"

        story_folder_name = re.sub(r'[\\/*?:"<>|]', "", story_name).strip()
        if not story_folder_name: story_folder_name = "Untitled_Story"
        try:
            save_dir_path = safe_story_path(Path(DATA_DIR) / "stories", story_folder_name)
        except ValueError as e:
            # Scraped <title> produced a traversal-like value. Fall back to
            # a timestamped name rather than writing outside the sandbox.
            logger.warning(f"Unsafe story folder {story_folder_name!r} rejected: {e}; using timestamped fallback")
            story_folder_name = f"Untitled_Story_{int(time.time())}"
            save_dir_path = safe_story_path(Path(DATA_DIR) / "stories", story_folder_name)
        save_dir_path.mkdir(parents=True, exist_ok=True)
        save_dir = str(save_dir_path)  # downstream os.path.join() callers expect str
        
        meta_file = os.path.join(save_dir, "metadata.json")
        if not os.path.exists(meta_file):
            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump({"title": story_name, "source": start_url}, f, ensure_ascii=False)
        
        crawl_jobs[job_id].update({
            "status": "running",
            "story_title": story_name,
            "total": max_chapters, 
            "current": 0,
            "message": "Starting crawl..."
        })
        _save_crawl_status()

        current_url = start_url
        count = 0
        
        # [NEW Logic] Logic: "List Mode" vs "Chain Mode"
        # If we have a `chapters` list and `start_url` is in it, use the list!
        chapter_iterator = None
        _dbg(f"chapters={len(chapters) if chapters else 0}, start_url={start_url}")
        if chapters and len(chapters) > 0:
            # Find index of start_url
            start_index = -1
            for i, c in enumerate(chapters):
                # Loose compare URLs (sometimes http vs https or trailing slash)
                if c['url'].strip('/') == start_url.strip('/'):
                    start_index = i
                    break
            
            if start_index != -1:
                _dbg(f"LIST MODE from index {start_index}/{len(chapters)}")
                logger.info(f"[{job_id}] Engaging 'List Mode' crawling starting from index {start_index} (total {len(chapters)}).")
                chapter_iterator = iter(chapters[start_index:])
            else:
                 _dbg(f"start_url NOT in chapter list, CHAIN MODE. first_url={chapters[0]['url'][:80]}")
                 logger.info(f"[{job_id}] Start URL not found in chapter list. Falling back to chain mode.")
        
        _dbg(f"Entering loop. chapter_iterator={'yes' if chapter_iterator else 'no'}, max={max_chapters}")
        while count < max_chapters:
            _dbg(f"Loop top: count={count}, status={crawl_jobs[job_id].get('status')}")
            # Check for stop signal
            if crawl_jobs[job_id].get("status") == "cancelled":
                logger.info(f"[{job_id}] Job cancelled by user.")
                crawl_jobs[job_id]["message"] = "Cancelled by user."
                _save_crawl_status()
                break
            
            if chapter_iterator:
                try:
                    chapter_node = next(chapter_iterator)
                    current_url = chapter_node['url']
                    # We could also use chapter_node['title'] to assume title, but fetching content confirms existence.
                except StopIteration:
                    logger.info(f"[{job_id}] End of chapter list reached.")
                    break
            
            # ... rest of fetch log ...

            logger.info(f"[{job_id}] Step {count+1}: Fetching {current_url}")
            _dbg(f"Fetching #{count+1}: {current_url[:80]}")
            resp_status: int | None = None
            resp_text = ""
            # [NEW] Retry Logic for 429: max 3 retries, 5s delay. Body
            # is streamed + capped at MAX_CHAPTER_BYTES to bound disk +
            # RAM use per chapter.
            for attempt in range(4): # Initial + 3 retries
                try:
                    resp_status, resp_text = get_capped_text(
                        current_url, headers=headers, timeout=(3, 10),
                    )
                    if resp_status == 429:
                        if attempt < 3:
                            logger.warning(f"[{job_id}] 429 Too Many Requests. Retrying in 5s... ({attempt+1}/3)")
                            time.sleep(5)
                            continue
                        else:
                            logger.error(f"[{job_id}] Max retries exceeded for 429.")
                    break # Success or non-retriable status
                except Exception as e:
                    logger.error(f"[{job_id}] Request failed: {e}")
                    resp_status = None
                    break

            if resp_status is None: break

            logger.info(f"[{job_id}] Status {resp_status} for {current_url}")
            if resp_status != 200:
                logger.error(f"[{job_id}] Failed to fetch {current_url}: {resp_status}")
                # Try next chapter if in list mode, otherwise break
                if chapter_iterator:
                    continue
                else:
                    break

            soup = BeautifulSoup(resp_text, "html.parser")
            logger.info(f"[{job_id}] Parsed HTML for {current_url}. Title selector: {title_selector}, Content: {content_selector}")
            
            # Content
            content_div = soup.select_one(content_selector)
            if not content_div:
                logger.warning(f"[{job_id}] Content selector '{content_selector}' not found in {current_url}. Trying fallbacks...")
                # Try simpler selectors?
                # ...
                # Fallback handled later in loop? 
                
            if not content_div:
                crawl_jobs[job_id]["message"] = f"No content found at {current_url}"
                # DO NOT BREAK yet, Try next chapter if list mode!
                if chapter_iterator:
                    logger.info(f"[{job_id}] skipping {current_url} due to no content.")
                    continue
                else:
                     break
                
            for s in content_div(["script", "style", "iframe"]):
                s.decompose()
            text = content_div.get_text(separator="\n\n").strip()
            logger.info(f"[{job_id}] Extracted content length: {len(text)}")
            
            # Title
            t_tag = soup.select_one(title_selector)
            chap_title = t_tag.get_text(strip=True) if t_tag else f"Chapter {count+1}"
            
            # Save
            filename = f"{count+1:03d}_{re.sub(r'[\\\\/*?:\"<>|]', '', chap_title)[:50]}.txt"
            with open(os.path.join(save_dir, filename), "w", encoding="utf-8") as f:
                f.write(text)
                
            count += 1
            crawl_jobs[job_id].update({
                "current": count,
                "message": f"Crawled: {chap_title}"
            })
            _save_crawl_status()
            _dbg(f"Saved #{count}: {chap_title}")
            
            # Next chapter: chain mode only (list mode uses iterator at top of loop)
            if not chapter_iterator:
                next_url = None
                if next_selector:
                    n = soup.select_one(next_selector)
                    if n and n.get("href"):
                        next_url = n.get("href")
                
                # Smart next detection if explicit selector failed or wasn't provided
                if not next_url:
                    # 1. Search for typical "Next" buttons by class/id
                    next_candidates = soup.select("a.next, a.chap-next, a#next_chap, a.btn-next, a[title*='sau'], a[title*='next']")
                    if next_candidates:
                        next_url = next_candidates[0]["href"]
                    else:
                        # 2. Look for keywords in text
                        for a in soup.find_all("a"):
                            t = a.get_text(strip=True).lower()
                            # TODO(i18n): VN literals — matches Vietnamese "next chapter" link text
                            if "chương sau" in t or "tiếp" in t or "next" in t or "chap sau" in t:
                                next_url = a.get("href")
                                logger.info(f"[{job_id}] Auto-detected next link: {t} -> {next_url}")
                                break
                
                if next_url:
                    if not next_url.startswith("http"):
                        next_url = urljoin(current_url, next_url)
                    logger.info(f"[{job_id}] Next URL found: {next_url}")
                else:
                    _dbg("CHAIN MODE: No next URL found. Stopping.")
                    logger.info(f"[{job_id}] No next URL found. Stopping.")
                
                if not next_url:
                    break
                    
                current_url = next_url
            # else: In List Mode, loop continues to `next(chapter_iterator)` at top
                
            _dbg(f"Sleeping {speed}s before next chapter...")
            time.sleep(speed)
            _dbg(f"Woke up, looping back")
            
        _dbg(f"Loop exited. count={count}, status={crawl_jobs[job_id].get('status')}")
        crawl_jobs[job_id]["status"] = "completed"
        crawl_jobs[job_id]["message"] = f"Completed. Saved {count} chapters."
        _save_crawl_status()
        logger.info(f"[{job_id}] Crawl completed. Total chapters: {count}")
        
    except Exception as e:
        _dbg(f"EXCEPTION: {e}\n{_tb.format_exc()}")
        logger.error(f"Crawl job {job_id} failed: {e}")
        crawl_jobs[job_id].update({
            "status": "failed",
            "message": str(e)
        })
        _save_crawl_status()

@mcp.tool()
def crawl_story_full(start_url: str, content_selector: str, title_selector: str = "h1", next_selector: str = None, speed: float = 1.0, max_chapters: int = 1000) -> str:
    """
    Start a background crawl job for a story.
    
    This tool crawls multiple chapters starting from `start_url`.
    It automatically detects if `start_url` is a Chapter or a Story page.
    
    Args:
        start_url: The URL to start crawling from. Can be a Chapter URL (e.g., .../chapter-1) or a Story URL (e.g., .../story-name).
                   If a Story URL is provided, it will attempt to find the chapter list and start from Chapter 1.
        content_selector: CSS selector to find the chapter content (e.g., 'div.chapter-c').
        title_selector: CSS selector for the chapter title. Defaults to 'h1'.
        next_selector: (Optional) CSS selector for the 'Next Chapter' link. 
                       If not provided, the crawler uses heuristics to find the next link.
        speed: Delay in seconds between requests to avoid blocking. Default is 1.0s.
        max_chapters: Maximum number of chapters to crawl. Default is 1000. 
                      Increase this if you expect the story to be longer.

    Returns:
        JSON string containing the `job_id` to track progress via `get_crawl_status(job_id)`.
    """
    import threading
    import uuid
    
    job_id = str(uuid.uuid4())
    crawl_jobs[job_id] = {
        "status": "pending",
        "story_title": "Initializing...",
        "current": 0,
        "total": max_chapters,
        "message": "Starting..."
    }
    _save_crawl_status()
    
    thread = threading.Thread(
        target=_crawl_worker,
        args=(job_id, start_url, content_selector, title_selector, next_selector, speed, max_chapters),
        daemon=True
    )
    thread.start()
    
    return json.dumps({"job_id": job_id})

@mcp.tool()
def cancel_crawl(job_id: str) -> str:
    """Cancel a running crawl job."""
    logger.info(f"Received cancel request for job: {job_id}")
    if job_id in crawl_jobs:
        crawl_jobs[job_id]["status"] = "cancelled"
        _save_crawl_status()
        logger.info(f"Job {job_id} marked as cancelled. Current Status: {crawl_jobs[job_id]}")
        return json.dumps({"status": "cancelled", "job_id": job_id})
    logger.warning(f"Cancel failed: Job {job_id} not found in {list(crawl_jobs.keys())}")
    return json.dumps({"status": "not_found", "job_id": job_id})

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


def _build_audio_url(book_id: str) -> Optional[str]:
    """Build relative audio URL for a given book_id."""
    return f"/api/tts/{quote(book_id, safe='')}"


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
                        # Compute book_id (same pattern as handle_read_local in server.py)
                        safe_fname = re.sub(r'[^\w\-\.]', '_', filename)
                        book_id = f"story_{dir_name}_{safe_fname}"
                        audio_url = _build_audio_url(book_id)
                        response_text = f"Now playing {dir_name} chapter {chapter_number}."
                        if audio_url:
                            response_text += f" [[[AUDIO_URL: {audio_url}]]]"
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
