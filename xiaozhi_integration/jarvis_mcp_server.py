"""
Jarvis MCP Server - Xiaozhi Integration

MCP Server exposing Jarvis AI capabilities to the Xiaozhi ESP32.
Used together with mcp_pipe.py to connect to Xiaozhi Cloud.

Features:
- Background task from the start to avoid duplicate API calls
- Waits up to 9s for the result (fits within Xiaozhi's 10s timeout)
- Polling mechanism to fetch the result if not finished yet

Usage:
    python jarvis_mcp_server.py
"""

import os
import sys
import re
import uuid
import time
import logging
import threading
import httpx
from fastmcp import FastMCP
from concurrent.futures import ThreadPoolExecutor, Future

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('JarvisMCP')

# Fix UTF-8 encoding for Windows console
if sys.platform == 'win32':
    sys.stderr.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')

# Configuration
JARVIS_API_URL = os.environ.get('JARVIS_API_URL', 'http://localhost:8000')
JARVIS_API_KEY = os.environ.get('JARVIS_API_KEY', '')
WAIT_TIMEOUT = 9.0  # Wait up to 9s (fits within Xiaozhi's 10s timeout)
API_TIMEOUT = 120.0  # Long timeout for the Jarvis API (2 minutes)

# Task storage for background tasks
# task_id -> {"status": "pending"|"done"|"error", "result": str, "message": str, "future": Future}
tasks = {}
task_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=10)

# Create MCP Server
mcp = FastMCP("Jarvis")


TAG_PATTERN = re.compile(r'\[\[\[AUDIO_URL:\s*(.*?)\]\]\]')


def _extract_audio_url(text: str) -> tuple[str, str | None]:
    """Extract [[[AUDIO_URL: ...]]] tag from text. Returns (cleaned_text, audio_url)."""
    match = TAG_PATTERN.search(text)
    if match:
        audio_url = match.group(1).strip()
        cleaned = text.replace(match.group(0), "").strip()
        return cleaned, audio_url
    return text, None


def call_jarvis_api(message: str) -> tuple[bool, str, str | None]:
    """
    Call the Jarvis API with a long timeout (not limited by the Xiaozhi timeout).
    Returns (success, result, audio_url).
    """
    try:
        headers = {"Content-Type": "application/json"}
        if JARVIS_API_KEY:
            headers["Authorization"] = f"Bearer {JARVIS_API_KEY}"

        logger.info(f"Calling Jarvis API: {message[:50]}...")

        with httpx.Client(timeout=API_TIMEOUT) as client:
            response = client.post(
                f"{JARVIS_API_URL}/api/chat",
                json={"message": message},
                headers=headers
            )

            if response.status_code == 200:
                data = response.json()
                result = data.get("response", "Sorry, I could not process this request.")
                logger.info(f"Jarvis API response: {result[:50]}...")
                # Get playback URL from API response (absolute URL for device)
                audio_url = data.get("playback_url")
                # Fallback: extract from [[[AUDIO_URL: ...]]] tag in text
                if not audio_url:
                    result, audio_url = _extract_audio_url(result)
                else:
                    # Clean any remaining tags from display text
                    result, _ = _extract_audio_url(result)
                if audio_url:
                    logger.info(f"Audio URL for device: {audio_url}")
                return True, result, audio_url
            else:
                return False, f"API Error: {response.status_code}", None

    except httpx.TimeoutException:
        return False, "Jarvis API timeout", None
    except Exception as e:
        # Log the full exception locally, but return a generic message to the
        # remote (Xiaozhi cloud → device → user). Raw exception strings can
        # leak file paths, internal types, and library internals.
        logger.exception("call_jarvis_api failed")
        return False, "Jarvis backend error", None


def process_task(task_id: str, message: str):
    """
    Process the task in the background - CALL THE JARVIS API EXACTLY ONCE
    """
    logger.info(f"Starting task {task_id}: {message[:50]}...")

    success, result, audio_url = call_jarvis_api(message)

    with task_lock:
        if task_id in tasks:
            if success:
                tasks[task_id]["status"] = "done"
                tasks[task_id]["result"] = result
                if audio_url:
                    tasks[task_id]["audio_url"] = audio_url
                logger.info(f"Task {task_id} completed: {result[:50]}...")
            else:
                tasks[task_id]["status"] = "error"
                tasks[task_id]["result"] = result
                logger.error(f"Task {task_id} failed: {result}")


def wait_for_task(task_id: str, timeout: float) -> dict | None:
    """
    Wait for the task to finish within the timeout window.
    Returns the result if the task finished, None if still pending.
    """
    poll_interval = 0.3  # Check every 0.3s
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        with task_lock:
            task = tasks.get(task_id)
            if task is None:
                return {"status": "not_found", "success": False, "response": "Task does not exist"}
            
            if task["status"] == "done":
                result = task["result"]
                audio_url = task.get("audio_url")
                del tasks[task_id]
                resp = {"status": "done", "success": True, "response": result}
                if audio_url:
                    resp["audio_url"] = audio_url
                    resp["instruction"] = "IMPORTANT: Play this audio on the device by calling self.media.play_url with the audio_url value."
                return resp
            
            if task["status"] == "error":
                result = task["result"]
                del tasks[task_id]
                return {"status": "error", "success": False, "response": result}
        
        # Still pending, keep waiting
        time.sleep(poll_interval)

    # Timed out while still pending
    return None


@mcp.tool()
def ping() -> dict:
    """
    Check the connection to Jarvis. Use this tool to test the connection.
    Returns immediately, no waiting required.
    """
    return {"success": True, "response": "Jarvis is ready!"}


@mcp.tool()
def ask_jarvis(message: str) -> dict:
    """
    Ask Jarvis AI - a versatile smart assistant.

    Jarvis can help you:
    • Search for information, news, weather
    • Look up finance: gold prices, stocks, coins
    • Manage Google Calendar and Gmail
    • Control smart home devices (Roborock vacuum robot)
    • Find and play YouTube music
    • Read stories and audiobooks from the library
    • Help with programming, GitHub
    • Answer questions intelligently with AI

    IMPORTANT: If the result contains a "task_id", Jarvis is still processing.
    You MUST keep calling check_task(task_id) until you receive the final result.

    IMPORTANT: If the result contains an "audio_url", you MUST call self.media.play_url(url=audio_url)
    to play the audio on the device. This is mandatory to play stories/music for the user.

    Args:
        message: The question or request to send to Jarvis

    Returns:
        - If handled quickly: {"success": True, "response": "..."}
        - If audio is included: {"success": True, "response": "...", "audio_url": "http://..."}
        - If more time is needed: {"success": True, "pending": True, "task_id": "...", "response": "Processing..."}

    Examples:
        - "What's the weather like today?"
        - "What's the gold price today?"
        - "Schedule a meeting tomorrow at 9am"
        - "Turn on the vacuum robot"
    """
    try:
        logger.info(f"Chat request: {message[:100]}...")
        
        # Create task ID
        task_id = str(uuid.uuid4())[:8]

        # Create the task entry BEFORE submitting
        with task_lock:
            tasks[task_id] = {
                "status": "pending",
                "result": None,
                "message": message
            }
        
        # Submit task to background executor - CALL THE API ONLY ONCE
        executor.submit(process_task, task_id, message)
        
        logger.info(f"Created task {task_id}, waiting up to {WAIT_TIMEOUT}s...")
        
        # Wait up to 9s for the result
        result = wait_for_task(task_id, WAIT_TIMEOUT)

        if result is not None:
            # Task finished (done or error)
            logger.info(f"Task {task_id} finished within timeout")
            return result
        else:
            # Task still pending after 9s -> return task_id for polling
            logger.info(f"Task {task_id} still pending, returning task_id for polling")
            return {
                "success": True,
                "pending": True,
                "task_id": task_id,
                "response": f"Jarvis is processing your request. Please call check_task with task_id='{task_id}' to get the result."
            }
                
    except Exception:
        # Generic response to the remote; full traceback stays in local logs.
        logger.exception("ask_jarvis failed")
        return {"success": False, "response": "Jarvis encountered an internal error."}


@mcp.tool()
def check_task(task_id: str) -> dict:
    """
    Check the status of a task being processed.

    IMPORTANT: You MUST call this tool immediately after receiving a task_id from ask_jarvis().
    If status is "pending", you MUST keep calling check_task until you receive the result.
    Do not stop until status is "done" or "error".

    IMPORTANT: If the result contains an "audio_url", you MUST call self.media.play_url(url=audio_url)
    to play the audio on the device.

    Args:
        task_id: ID of the task to check (received from ask_jarvis())

    Returns:
        - If still processing: {"status": "pending", "response": "Still processing..."}
        - If finished: {"status": "done", "response": "Result..."}
        - If audio is included: {"status": "done", "response": "...", "audio_url": "http://..."}
        - If failed: {"status": "error", "response": "Error..."}
        - If not found: {"status": "not_found", "response": "Task does not exist"}

    Examples:
        check_task("a1b2c3d4") -> {"status": "done", "response": "Today's gold price is..."}
    """
    logger.info(f"Check task: {task_id}")
    
    # Check that the task_id exists
    with task_lock:
        if task_id not in tasks:
            return {
                "status": "not_found",
                "success": False,
                "response": f"Task '{task_id}' does not exist. Please double-check the task_id."
            }

    # Wait up to 9s for the result (fits within Xiaozhi's 10s timeout)
    result = wait_for_task(task_id, WAIT_TIMEOUT)

    if result is not None:
        # Task finished (done, error, or not_found)
        logger.info(f"Task {task_id} finished: {result['status']}")
        return result
    else:
        # Task still pending after 9s
        logger.info(f"Task {task_id} still pending after {WAIT_TIMEOUT}s")
        return {
            "status": "pending",
            "success": True,
            "response": "Jarvis is still processing. Please call check_task again to keep waiting for the result."
        }


if __name__ == "__main__":
    logger.info("Starting Jarvis MCP Server...")
    logger.info(f"Jarvis API URL: {JARVIS_API_URL}")
    logger.info(f"Wait timeout: {WAIT_TIMEOUT}s, API timeout: {API_TIMEOUT}s")
    mcp.run(transport="stdio")
