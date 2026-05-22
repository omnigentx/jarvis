# Xiaozhi MCP Integration

> 🚧 **Status: Inactive (paused).** This module is not currently being developed. Code is left in-tree for reference; **no security patches will be applied** to its dependencies until work resumes. Run at your own risk if you fork this directory standalone. The Jarvis backend deploy does not load these packages — vulnerabilities only matter when running the standalone xiaozhi MCP server.

Bridges a [Xiaozhi](https://xiaozhi.me) ESP32 voice device with the Jarvis backend over the Model Context Protocol (MCP). The Xiaozhi device sends a voice request to Xiaozhi Cloud; Cloud forwards it to a local MCP server (`jarvis_mcp_server.py`) over a WebSocket relay (`mcp_pipe.py`); the MCP server calls the Jarvis HTTP API and returns the answer (plus optional audio URL for media playback).

## Architecture

```
Xiaozhi ESP32 → Xiaozhi Cloud → mcp_pipe.py → jarvis_mcp_server.py → Jarvis API
                  (WebSocket)      (stdio)         (HTTP)
```

## Setup & Run

1. **Create your `.env` file:**

   ```bash
   cd xiaozhi_integration
   cp .env.example .env
   ```

   Then fill in:
   - `MCP_ENDPOINT` — from the Xiaozhi mobile app (each endpoint is device-specific and contains a JWT — treat it as a secret).
   - `JARVIS_API_KEY` — **must match** the `JARVIS_API_KEY` in your `backend/.env`. This is the Jarvis master Bearer token. **Do not use the placeholder value** shipped in `.env.example`.
   - `JARVIS_API_URL` — where the Jarvis backend is reachable from this machine (`http://localhost:8000` for same host, `http://host.docker.internal:8000` for Docker, or a LAN/VPN IP).

2. **Start the Jarvis backend** (terminal 1):

   ```bash
   cd backend
   uv run uvicorn server:app --host 0.0.0.0 --port 8000
   ```

3. **Start the MCP pipe** (terminal 2):

   ```bash
   cd xiaozhi_integration
   uv run python mcp_pipe.py jarvis_mcp_server.py
   ```

## Tools exposed to Xiaozhi

### `ping()`
Connectivity check. Returns immediately.

### `ask_jarvis(message: str)`
Sends a free-form request to Jarvis AI. Jarvis can answer questions, look up information (weather, news, finance), manage Google Calendar / Gmail, control smart-home devices, play YouTube / audiobooks, and assist with coding — whichever capabilities are enabled in your backend.

Long-running requests return a `task_id`; poll with `check_task(task_id)` until `status` is `done` or `error`. Responses may include an `audio_url` for the device to stream.

### `check_task(task_id: str)`
Polls for the result of a long-running `ask_jarvis` call.

**Example:** Say to Xiaozhi *"Ask Jarvis what the weather is today."*

## Notes on logs and privacy

- `jarvis_mcp_server.py` logs the first ~100 characters of each user request and response at `INFO` level (stderr → stdout of the pipe process). These transcripts may include sensitive voice content. **Redact before sharing logs in bug reports.**
- Set `LOG_LEVEL=WARNING` (or comment out the `logger.info(...)` lines) if you want a quieter, privacy-preserving run.
- Exception details are intentionally redacted from responses sent to Xiaozhi Cloud — full tracebacks stay in local logs only.

## Notes on the API endpoint

- This integration uses the **legacy** `POST /api/chat` endpoint (single-shot, non-streaming). The newer dashboard uses `POST /api/chat-stream` (SSE) and rich agent state, which Xiaozhi devices cannot consume.
- An empty `JARVIS_API_KEY` (on both backend and pipe) puts the backend in **unauthenticated mode** — fine for first-boot setup, never for any host reachable beyond `localhost`.

## Compatibility

- Python `>=3.13` is currently pinned in `pyproject.toml`. The code does not actually require 3.13-only features; relax to `>=3.11` if you need to run on an older interpreter.
