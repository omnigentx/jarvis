import logging
import json
import os
import re
from mcp.server.fastmcp import FastMCP

# Logging — inherits config from centralized logging_config
logger = logging.getLogger("library_server")

# Data Paths
# Assumes tool is run from backend root via `uv run tools/library_server.py`
DATA_DIR = "data"

# Add backend dir to sys.path for cross-module imports (MCP subprocess context)
import sys
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from helpers.audio_cache import get_audio_cache_path

# Initialize FastMCP server
mcp = FastMCP("LibraryServer")


@mcp.tool()
def local_search(query: str) -> str:
    """
    Search library for stories or chapters matching the query term.
    Returns JSON list of matching entries with id, title, chapter, status.
    """
    stories_dir = os.path.join(DATA_DIR, "stories")
    if not os.path.exists(stories_dir):
        return json.dumps([])

    try:
        matches = []
        query_norm = query.lower().strip()
        
        # Iterate over all stories
        for story_name in os.listdir(stories_dir):
            story_path = os.path.join(stories_dir, story_name)
            if not os.path.isdir(story_path) or story_name.startswith('.'):
                continue

            # Check if story name matches
            if query_norm in story_name.lower():
                 matches.append({
                    "id": story_name,
                    "title": story_name,
                    "type": "story",
                    "status": "available"
                })

            # Check chapters
            for filename in os.listdir(story_path):
                if filename.endswith(".txt") and not filename.startswith("."):
                    chapter_name = filename.replace(".txt", "")
                    if query_norm in chapter_name.lower():
                        matches.append({
                            "id": filename,
                            "title": story_name,
                            "chapter": chapter_name,
                            "type": "chapter",
                            "status": "available"
                        })
                        if len(matches) >= 10: break # Limit results
            
            if len(matches) >= 10: break

        return json.dumps(matches, ensure_ascii=False)
        
    except Exception as e:
        logger.error(f"Error searching library: {e}")
        return json.dumps({"error": str(e)})

@mcp.tool()
def local_list_stories() -> str:
    """
    List all unique story titles currently in the personal library (folders in data/stories).
    Use this to see what stories are available for reading.
    Returns JSON list of story names.
    """
    stories_dir = os.path.join(DATA_DIR, "stories")
    
    if not os.path.exists(stories_dir):
        return json.dumps([])

    try:
        # List subdirectories in stories_dir
        stories = [
            d for d in os.listdir(stories_dir) 
            if os.path.isdir(os.path.join(stories_dir, d)) and not d.startswith('.')
        ]
        return json.dumps(sorted(stories), ensure_ascii=False)
        
    except Exception as e:
        logger.error(f"Error listing stories: {e}")
        return json.dumps({"error": str(e)})

@mcp.tool()
def local_list_chapters(story_title: str) -> str:
    """
    List all chapters available for a specific story (text files in data/stories/{StoryName}).
    Returns JSON list with chapter names, file IDs, and status.
    """
    story_dir = os.path.join(DATA_DIR, "stories", story_title)
    
    if not os.path.exists(story_dir):
        return json.dumps([])

    try:
        matches = []
        for filename in os.listdir(story_dir):
            if filename.endswith(".txt") and not filename.startswith("."):
                # Clean up filename for display
                # Format: 001_ChapterName.txt -> ChapterName
                chapter_name = filename.replace(".txt", "")
                
                # Check if audio exists in audio_cache (hash-based)
                try:
                    with open(os.path.join(story_dir, filename), "r", encoding="utf-8") as tf:
                        raw = tf.read().strip()
                    audio_path = get_audio_cache_path(raw)
                    status = "ready" if os.path.exists(audio_path) else "text_only"
                except:
                    status = "text_only"
                
                matches.append({
                    "chapter": chapter_name,
                    "id": filename,
                    "status": status,
                    "path": os.path.join(story_dir, filename)
                })
        
        # Sort by chapter name (try to extract number)
        def extract_number(s):
            # Extract first number found
            m = re.search(r'\d+', s)
            return int(m.group()) if m else 999999
            
        matches.sort(key=lambda x: extract_number(x["chapter"]))
        return json.dumps(matches, ensure_ascii=False)
        
    except Exception as e:
        logger.error(f"Error listing chapters: {e}")
        return json.dumps({"error": str(e)})

if __name__ == "__main__":
    mcp.run()

