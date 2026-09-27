"""Local OpenAI-compatible provider that records model IDs for E2E tests.

Run with MODEL_PROBE_LOG=/tmp/model-probe.jsonl python tests/e2e/model_selection_mock_provider.py.
Only request metadata is recorded; prompts and credentials are discarded.
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


LOG_PATH = Path(os.environ.get("MODEL_PROBE_LOG", "/tmp/model-probe.jsonl"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path.rstrip("/") != "/v1/models":
            self.send_error(404)
            return
        body = json.dumps({
            "object": "list",
            "data": [
                {"id": name, "object": "model", "owned_by": "local-test"}
                for name in ("gpt-4o-mini", "gpt-4o")
            ],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        model = request.get("model")
        if model not in ("gpt-4o-mini", "gpt-4o"):
            self.send_error(400, "unsupported test model")
            return
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(json.dumps({
                "time": time.time(),
                "model": model,
                "stream": bool(request.get("stream")),
                "message_count": len(request.get("messages") or []),
            }) + "\n")

        base = {"id": "model-probe", "created": int(time.time()), "model": model}
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for choice in (
                {"index": 0, "delta": {"role": "assistant", "content": "OK"}, "finish_reason": None},
                {"index": 0, "delta": {}, "finish_reason": "stop"},
            ):
                chunk = {**base, "object": "chat.completion.chunk", "choices": [choice]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            body = json.dumps({
                **base,
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8318), Handler).serve_forever()
