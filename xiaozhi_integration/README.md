# Xiaozhi MCP Integration

Tích hợp Xiaozhi ESP32 với Jarvis Backend thông qua MCP (Model Context Protocol).

## Kiến Trúc

```
Xiaozhi ESP32 → Xiaozhi Cloud → mcp_pipe.py → jarvis_mcp_server.py → Jarvis API
```

## Cài Đặt & Chạy

1. **Tạo file `.env`:**
   ```bash
   cd xiaozhi_integration
   cp .env.example .env
   # Điền MCP_ENDPOINT từ app Xiaozhi
   ```

2. **Chạy Jarvis Backend** (terminal 1):
   ```bash
   cd backend
   uv run uvicorn server:app --host 0.0.0.0 --port 8000
   ```

3. **Chạy MCP Pipe** (terminal 2):
   ```bash
   cd xiaozhi_integration
   uv run python mcp_pipe.py jarvis_mcp_server.py
   ```

## Tool: `jarvis.chat`

Gọi Jarvis AI với bất kỳ yêu cầu nào:
- Tìm kiếm thông tin, thời tiết
- Tra cứu tài chính (vàng, cổ phiếu, coin)
- Quản lý lịch, email
- Điều khiển nhà thông minh
- Phát nhạc YouTube
- Đọc truyện/sách nói

**Ví dụ:** Nói với Xiaozhi: *"Hỏi Jarvis hôm nay thời tiết thế nào?"*
