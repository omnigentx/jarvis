"""Base class for cloud STT backends that stream PCM over a WebSocket.

The scaffolding here is provider-agnostic: thread + asyncio loop hand-off,
audio queue, hook fan-out, reconnect with exponential backoff, shutdown
plumbing. Each cloud provider (Soniox, Deepgram, AssemblyAI, ...)
subclasses :class:`WSStreamingSTT` and supplies three small bits:

* ``WS_URL`` — endpoint to connect to
* ``_build_config_message()`` — JSON config sent immediately after connect
* ``_handle_event(data)`` — parse one inbound message and emit events
  (``self._emit_partial``, ``self._emit_final``, ``self._emit_endpoint``)

Why a base class instead of free helper functions: the per-instance state
(hook ref, audio queue, asyncio loop) is the awkward part to share, and a
base class keeps the public surface (``feed_audio`` / ``set_hook`` /
``start_listen_loop`` / ``shutdown``) identical across providers — the
voice WS route never has to special-case which cloud STT produced the
service.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Callable, ClassVar, Optional

logger = logging.getLogger(__name__)


EventHook = Callable[[str, dict[str, Any]], None]


class WSStreamingSTT:
    """Common scaffolding for WebSocket-based cloud STT providers."""

    #: Override in subclass — ``wss://...`` endpoint URL.
    WS_URL: ClassVar[str] = ""

    #: Override in subclass — logging tag, e.g. ``"Soniox STT"``.
    LOG_TAG: ClassVar[str] = "WS STT"

    def __init__(self, *, sample_rate: int = 16000) -> None:
        if not self.WS_URL:
            raise NotImplementedError(f"{type(self).__name__} must set WS_URL")
        self._sample_rate = int(sample_rate)

        self._hook: Optional[EventHook] = None
        self._lock = threading.Lock()
        self._closed = False

        self._audio_queue: Optional[asyncio.Queue[bytes]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    # ---- public RealtimeSTTService-compatible surface ---------------------

    def set_hook(self, hook: Optional[EventHook]) -> None:
        with self._lock:
            self._hook = hook

    def feed_audio(self, pcm_chunk: bytes) -> None:
        if self._closed or self._loop is None or self._audio_queue is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, pcm_chunk)
        except RuntimeError:
            return

    def start_listen_loop(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        t = threading.Thread(
            target=self._thread_runner,
            name=f"{type(self).__name__}-loop",
            daemon=True,
        )
        self._thread = t
        t.start()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop is not None and self._audio_queue is not None:
            try:
                self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, b"")
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # ---- subclass extension points ---------------------------------------

    def _build_config_message(self) -> dict[str, Any]:
        """Return the JSON object sent immediately after connecting."""
        raise NotImplementedError

    def _handle_event(self, data: dict[str, Any]) -> None:
        """Parse one inbound JSON message and emit events as appropriate."""
        raise NotImplementedError

    def _on_connected(self) -> None:
        """Hook for subclasses to reset per-session accumulators on
        (re)connect. Called once after the config message is sent.
        """

    # ---- emit helpers (subclass convenience) -----------------------------

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        with self._lock:
            hook = self._hook
        if hook is None:
            return
        try:
            hook(event, payload)
        except Exception:
            logger.exception("[%s] hook raised on %s", self.LOG_TAG, event)

    def _emit_partial(self, text: str) -> None:
        if text and text.strip():
            self._emit("partial_transcript", {"text": text})

    def _emit_final(self, text: str) -> None:
        if text and text.strip():
            self._emit("final_transcript", {"text": text})

    def _emit_endpoint(self) -> None:
        """Signal end-of-utterance — mirrors RealtimeSTT's ``recording_stop``
        followed immediately by ``recording_start`` so downstream consumers
        (voice WS route) reset their per-turn state.
        """
        self._emit("vad_stop", {})
        self._emit("recording_stop", {})
        self._emit("recording_start", {})

    # ---- internals -------------------------------------------------------

    def _thread_runner(self) -> None:
        try:
            asyncio.run(self._run_ws())
        except Exception:
            logger.exception("[%s] WS thread exited with error", self.LOG_TAG)

    async def _run_ws(self) -> None:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover — present via uv.lock
            logger.error("[%s] websockets package not available: %s", self.LOG_TAG, exc)
            self._emit("error", {"detail": "websockets package not installed"})
            return

        self._loop = asyncio.get_running_loop()
        self._audio_queue = asyncio.Queue()

        backoff = 1.0
        while not self._closed:
            try:
                async with websockets.connect(
                    self.WS_URL,
                    max_size=None,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    await ws.send(json.dumps(self._build_config_message()))
                    self._on_connected()
                    self._emit("recording_start", {})
                    backoff = 1.0
                    await asyncio.gather(self._sender(ws), self._receiver(ws))
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._closed:
                    return
                logger.exception(
                    "[%s] WS loop crashed; reconnecting in %.1fs",
                    self.LOG_TAG, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    async def _sender(self, ws) -> None:
        assert self._audio_queue is not None
        while True:
            chunk = await self._audio_queue.get()
            if self._closed or chunk == b"":
                # Most cloud STT providers accept an empty string to signal
                # end-of-audio; subclasses can override _send_end_of_audio
                # if their protocol differs.
                try:
                    await self._send_end_of_audio(ws)
                except Exception:
                    pass
                return
            try:
                await ws.send(chunk)
            except Exception:
                return

    async def _send_end_of_audio(self, ws) -> None:
        await ws.send("")

    async def _receiver(self, ws) -> None:
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                continue
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                logger.debug("[%s] non-JSON message ignored", self.LOG_TAG)
                continue
            self._handle_event(data)
