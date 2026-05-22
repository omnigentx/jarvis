"""
Jarvis MCP Server - Xiaozhi Integration

MCP Server expose Jarvis AI capabilities cho Xiaozhi ESP32.
Sử dụng với mcp_pipe.py để kết nối với Xiaozhi Cloud.

Features:
- Background task ngay từ đầu để tránh duplicate API calls
- Chờ tối đa 9s cho kết quả (fit trong 10s timeout của Xiaozhi)
- Polling mechanism để lấy kết quả nếu chưa xong

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
WAIT_TIMEOUT = 9.0  # Chờ tối đa 9s (fit trong 10s timeout của Xiaozhi)
API_TIMEOUT = 120.0  # Timeout dài cho Jarvis API (2 phút)

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
    Call Jarvis API với timeout dài (không giới hạn bởi Xiaozhi timeout)
    Trả về (success, result, audio_url)
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
                result = data.get("response", "Xin lỗi, tôi không thể xử lý yêu cầu này.")
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
    Xử lý task trong background - GỌI JARVIS API MỘT LẦN DUY NHẤT
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
    Chờ task hoàn thành trong khoảng thời gian timeout.
    Trả về kết quả nếu task hoàn thành, None nếu còn pending.
    """
    poll_interval = 0.3  # Check mỗi 0.3s
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        with task_lock:
            task = tasks.get(task_id)
            if task is None:
                return {"status": "not_found", "success": False, "response": "Task không tồn tại"}
            
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
        
        # Vẫn pending, chờ thêm
        time.sleep(poll_interval)
    
    # Timeout mà vẫn pending
    return None


@mcp.tool()
def ping() -> dict:
    """
    Kiểm tra kết nối với Jarvis. Dùng tool này để test connection.
    Trả về ngay lập tức, không cần chờ.
    """
    return {"success": True, "response": "Jarvis đã sẵn sàng!"}


@mcp.tool()
def ask_jarvis(message: str) -> dict:
    """
    Hỏi Jarvis AI - Trợ lý thông minh đa năng.
    
    Jarvis có thể giúp bạn:
    • Tìm kiếm thông tin, tin tức, thời tiết
    • Tra cứu tài chính: giá vàng, cổ phiếu, coin
    • Quản lý lịch Google Calendar, email Gmail
    • Điều khiển nhà thông minh (robot hút bụi Roborock)
    • Tìm và phát nhạc YouTube
    • Đọc truyện, sách nói từ thư viện
    • Hỗ trợ lập trình, GitHub
    • Trả lời câu hỏi thông minh với AI
    
    QUAN TRỌNG: Nếu kết quả trả về có "task_id", nghĩa là Jarvis đang xử lý.
    Bạn PHẢI tiếp tục gọi check_task(task_id) cho đến khi nhận được kết quả cuối cùng.

    QUAN TRỌNG: Nếu kết quả có "audio_url", bạn PHẢI gọi self.media.play_url(url=audio_url)
    để phát audio trên thiết bị. Đây là bắt buộc để phát truyện/nhạc cho người dùng.

    Args:
        message: Câu hỏi hoặc yêu cầu gửi đến Jarvis

    Returns:
        - Nếu xử lý nhanh: {"success": True, "response": "..."}
        - Nếu có audio: {"success": True, "response": "...", "audio_url": "http://..."}
        - Nếu cần thời gian: {"success": True, "pending": True, "task_id": "...", "response": "Đang xử lý..."}
    
    Examples:
        - "Hôm nay thời tiết thế nào?"
        - "Giá vàng hôm nay bao nhiêu?"
        - "Đặt lịch họp ngày mai lúc 9h"
        - "Bật robot hút bụi"
    """
    try:
        logger.info(f"Chat request: {message[:100]}...")
        
        # Tạo task ID
        task_id = str(uuid.uuid4())[:8]
        
        # Tạo task entry TRƯỚC khi submit
        with task_lock:
            tasks[task_id] = {
                "status": "pending",
                "result": None,
                "message": message
            }
        
        # Submit task to background executor - CHỈ GỌI API MỘT LẦN
        executor.submit(process_task, task_id, message)
        
        logger.info(f"Created task {task_id}, waiting up to {WAIT_TIMEOUT}s...")
        
        # Chờ tối đa 9s cho kết quả
        result = wait_for_task(task_id, WAIT_TIMEOUT)
        
        if result is not None:
            # Task đã hoàn thành (done hoặc error)
            logger.info(f"Task {task_id} finished within timeout")
            return result
        else:
            # Task vẫn pending sau 9s -> trả task_id để polling
            logger.info(f"Task {task_id} still pending, returning task_id for polling")
            return {
                "success": True,
                "pending": True,
                "task_id": task_id,
                "response": f"Jarvis đang xử lý yêu cầu của bạn. Vui lòng gọi check_task với task_id='{task_id}' để lấy kết quả."
            }
                
    except Exception:
        # Generic response to the remote; full traceback stays in local logs.
        logger.exception("ask_jarvis failed")
        return {"success": False, "response": "Jarvis encountered an internal error."}


@mcp.tool()
def check_task(task_id: str) -> dict:
    """
    Kiểm tra trạng thái của task đang xử lý.
    
    QUAN TRỌNG: Bạn PHẢI gọi ngay tool này khi nhận được task_id từ ask_jarvis().
    Nếu status là "pending", bạn PHẢI tiếp tục gọi lại check_task cho đến khi nhận được kết quả.
    Không dừng lại cho đến khi status là "done" hoặc "error".

    QUAN TRỌNG: Nếu kết quả có "audio_url", bạn PHẢI gọi self.media.play_url(url=audio_url)
    để phát audio trên thiết bị.

    Args:
        task_id: ID của task cần kiểm tra (nhận từ ask_jarvis())

    Returns:
        - Nếu đang xử lý: {"status": "pending", "response": "Vẫn đang xử lý..."}
        - Nếu hoàn thành: {"status": "done", "response": "Kết quả..."}
        - Nếu có audio: {"status": "done", "response": "...", "audio_url": "http://..."}
        - Nếu lỗi: {"status": "error", "response": "Lỗi..."}
        - Nếu không tìm thấy: {"status": "not_found", "response": "Task không tồn tại"}
    
    Examples:
        check_task("a1b2c3d4") -> {"status": "done", "response": "Giá vàng hôm nay là..."}
    """
    logger.info(f"Check task: {task_id}")
    
    # Kiểm tra task_id tồn tại
    with task_lock:
        if task_id not in tasks:
            return {
                "status": "not_found",
                "success": False,
                "response": f"Task '{task_id}' không tồn tại. Vui lòng kiểm tra lại task_id."
            }
    
    # Chờ tối đa 9s cho kết quả (fit trong 10s timeout của Xiaozhi)
    result = wait_for_task(task_id, WAIT_TIMEOUT)
    
    if result is not None:
        # Task đã hoàn thành (done, error, hoặc not_found)
        logger.info(f"Task {task_id} finished: {result['status']}")
        return result
    else:
        # Task vẫn pending sau 9s
        logger.info(f"Task {task_id} still pending after {WAIT_TIMEOUT}s")
        return {
            "status": "pending",
            "success": True,
            "response": "Jarvis vẫn đang xử lý. Vui lòng gọi lại check_task để tiếp tục chờ kết quả."
        }


if __name__ == "__main__":
    logger.info("Starting Jarvis MCP Server...")
    logger.info(f"Jarvis API URL: {JARVIS_API_URL}")
    logger.info(f"Wait timeout: {WAIT_TIMEOUT}s, API timeout: {API_TIMEOUT}s")
    mcp.run(transport="stdio")
