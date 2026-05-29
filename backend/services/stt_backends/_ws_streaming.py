"""Base class for cloud STT backends that stream PCM over a WebSocket.

The scaffolding here is provider-agnostic: thread + asyncio loop hand-off,
audio queue, hook fan-out, mic-driven lifecycle (``pause`` / ``resume``),
infinite reconnect with exponential backoff, ``ws_status`` event emission,
shutdown plumbing. Each cloud provider (Soniox, Deepgram, AssemblyAI, …)
subclasses :class:`WSStreamingSTT` and supplies three small bits:

* ``WS_URL`` — endpoint to connect to
* ``_build_config_message()`` — JSON config sent immediately after connect
* ``_handle_event(data)`` — parse one inbound message and emit events
  (``self._emit_partial``, ``self._emit_final``, ``self._emit_endpoint``)

Optionally ``_on_connected()`` is called on every (re)connect so subclasses
can clear per-session accumulators.

Why a base class instead of free helper functions: the per-instance state
(hook ref, audio queue, asyncio loop, connection-state machine) is the
awkward part to share, and a base class keeps the public surface
(:class:`services.stt_backends.types.STTServiceProtocol`) identical across
providers — the voice WS route never has to special-case which cloud STT
produced the service.

Lifecycle vs Soniox 408 idle timeout
------------------------------------

Soniox closes idle WS with HTTP 408 after ~10 min of no audio. Pre-fix the
base class exited the reconnect loop on ANY clean close — singleton lived
on with a dead inner socket; user audio silently dropped on next utterance.

Fix is two-pronged:

1. **Mic-driven lifecycle**: ``ws_voice`` calls ``pause()`` when the
   frontend disconnects → we close the upstream WS cleanly → Soniox never
   has an idle window. On next mic-on, ``resume()`` opens a fresh WS.

2. **Infinite reconnect**: even if the WS does drop mid-session (network
   blip, Soniox restart), the outer ``while not self._closed`` re-enters
   for both normal close AND exception paths. Only ``shutdown()`` ends
   the loop. Backoff caps at 10 s; after ``ERROR_CONSECUTIVE_FAILURES``
   bad attempts in a row we emit ``ws_status: error`` so the route can
   surface a real failure to the user instead of spinning silently.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, ClassVar, Optional

from services.stt_backends.types import (
    EventHook,
    STTConnectionState,
)

logger = logging.getLogger(__name__)


#: After this many back-to-back failed connect attempts the service
#: transitions to ``ERROR`` so the route can surface a real problem to the
#: user. Picked so a ~30 s outage (sleep 0.5+1+2+4+8 = 15.5 s on 5 attempts)
#: still surfaces while a single transient blip doesn't.
ERROR_CONSECUTIVE_FAILURES = 5

#: Reconnect backoff bounds (seconds). Start tiny so a normal 408 cycles
#: back fast; cap at 10 s so a long outage doesn't burn the user.
BACKOFF_INITIAL = 0.5
BACKOFF_MAX = 10.0


class WSStreamingSTT:
    """Common scaffolding for WebSocket-based cloud STT providers.

    Implements :class:`services.stt_backends.types.STTServiceProtocol`.
    """

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

        # ── State machine ────────────────────────────────────────────────
        # ``_active_event`` gates the connect attempts inside ``_run_ws``.
        # We start INACTIVE — the route's ``resume()`` call on frontend
        # connect is what opens the upstream WS. That way the singleton
        # can sit around between voice sessions without holding a Soniox
        # socket open and burning its idle timer.
        self._active_event: Optional[asyncio.Event] = None  # bound to loop in _run_ws
        self._connection_state: STTConnectionState = STTConnectionState.IDLE
        self._connection_count = 0
        self._consecutive_failures = 0
        # Held by ``pause()`` so it can force-close the live WS to break
        # ``_receiver``'s async-for and let the outer loop fall through to
        # the ``await _active_event.wait()`` gate.
        self._current_ws_holder: dict[str, Any] = {"ws": None}

    # ---- public STTServiceProtocol surface ---------------------------

    def set_hook(self, hook: Optional[EventHook]) -> None:
        """Install / clear the event hook. Replays current
        ``connection_state`` to the new hook so a late subscriber (e.g.
        page reload mid-utterance) sees the right chip immediately
        instead of staying "idle" until the next transition.
        """
        with self._lock:
            self._hook = hook
        if hook is not None:
            self._emit("ws_status", {
                "state": self._connection_state.value,
                "attempt": self._consecutive_failures,
                "detail": "hook attached — replay current state",
            })

    def feed_audio(self, pcm_chunk: bytes) -> None:
        if self._closed or self._loop is None or self._audio_queue is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, pcm_chunk)
        except RuntimeError:
            return

    def start_listen_loop(self) -> None:
        """Boot the background WS thread. Idempotent. The connection
        won't actually open until ``resume()`` flips the active event —
        keeps the singleton cheap to construct.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        t = threading.Thread(
            target=self._thread_runner,
            name=f"{type(self).__name__}-loop",
            daemon=True,
        )
        self._thread = t
        t.start()

    def resume(self) -> None:
        """Open the upstream WS. Drives IDLE → CONNECTING → CONNECTED.
        Idempotent — safe to call when already CONNECTED.

        Thread-safe: the active event lives on the WS thread's loop;
        we schedule the ``.set()`` cross-thread.
        """
        if self._closed:
            logger.warning("[%s] resume() called after shutdown — ignored", self.LOG_TAG)
            return
        if self._loop is None or self._active_event is None:
            # Thread hasn't booted yet — start_listen_loop wasn't called
            # before resume. Boot it now; the runner will pick up the
            # active flag we'll set below as soon as the loop is alive.
            self.start_listen_loop()
            # Spin briefly so the runner can attach _loop + _active_event.
            for _ in range(50):  # ~500 ms cap
                if self._loop is not None and self._active_event is not None:
                    break
                threading.Event().wait(0.01)
            if self._loop is None or self._active_event is None:
                logger.error("[%s] resume(): WS thread failed to initialise", self.LOG_TAG)
                self._set_state(STTConnectionState.ERROR, detail="thread init failed")
                return

        loop = self._loop
        active = self._active_event
        try:
            loop.call_soon_threadsafe(active.set)
        except RuntimeError:
            # Loop already closed — service is effectively dead.
            self._set_state(STTConnectionState.ERROR, detail="loop closed")

    def pause(self) -> None:
        """Close the upstream WS cleanly. Drives → IDLE.
        Idempotent — safe to call when already IDLE.
        """
        if self._loop is None or self._active_event is None:
            return  # Nothing to pause

        loop = self._loop
        active = self._active_event

        def _do_pause():
            active.clear()
            ws = self._current_ws_holder.get("ws")
            if ws is not None:
                # Schedule close on the same loop; can't await from sync.
                asyncio.create_task(_close_ws_quietly(ws))

        try:
            loop.call_soon_threadsafe(_do_pause)
        except RuntimeError:
            pass

    def shutdown(self) -> None:
        """Permanently destroy. Cannot be resumed."""
        if self._closed:
            return
        self._closed = True

        # Wake up any blocked _active_event.wait() so the loop can see
        # _closed and exit cleanly.
        if self._loop is not None and self._active_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._active_event.set)
            except RuntimeError:
                pass

        # Push sentinel to break the audio sender.
        if self._loop is not None and self._audio_queue is not None:
            try:
                self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, b"")
            except RuntimeError:
                pass

        if self._thread is not None:
            self._thread.join(timeout=2.0)

        self._set_state(STTConnectionState.IDLE, detail="shutdown")

    # ── State accessors ──────────────────────────────────────────────

    @property
    def is_alive(self) -> bool:
        """True when the service can still produce transcripts (worker
        thread alive, not shut down, not in terminal ERROR).
        """
        if self._closed:
            return False
        if self._connection_state == STTConnectionState.ERROR:
            return False
        if self._thread is None:
            return False
        return self._thread.is_alive()

    @property
    def connection_state(self) -> STTConnectionState:
        return self._connection_state

    # ---- subclass extension points -----------------------------------

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

    # ---- emit helpers (subclass convenience) -------------------------

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
        """Signal end-of-utterance.

        Emits ``vad_stop`` + ``recording_stop``. Subclasses that detect
        utterance-start should emit ``recording_start`` from
        ``_handle_event`` on the first non-empty frame (see Soniox).
        Re-emitting it here used to cause a destructive race with the
        voice WS route: ``final_transcript`` arrived first → dispatch
        scheduled a new agent turn → the trailing ``recording_start``
        then ran ``_cancel_inflight`` because ``bot_speaking`` for the
        just-cancelled OLD turn hadn't flipped False yet → the *new*
        agent task got cancelled before it could run.
        """
        self._emit("vad_stop", {})
        self._emit("recording_stop", {})

    def _set_state(
        self,
        state: STTConnectionState,
        *,
        attempt: int | None = None,
        detail: str = "",
    ) -> None:
        """Transition the state machine + emit ``ws_status``.

        Only emits if the state actually changed — keeps the wire quiet
        when the receiver is just churning frames in CONNECTED.
        """
        if state == self._connection_state and detail == "":
            return
        self._connection_state = state
        self._emit("ws_status", {
            "state": state.value,
            "attempt": self._consecutive_failures if attempt is None else attempt,
            "detail": detail,
        })

    # ---- internals ---------------------------------------------------

    def _thread_runner(self) -> None:
        try:
            asyncio.run(self._run_ws())
        except Exception:
            logger.exception("[%s] WS thread exited with error", self.LOG_TAG)

    async def _run_ws(self) -> None:
        """The reconnect/lifecycle loop.

        Outer invariant: loop runs forever until ``self._closed``.
        Inside each iteration we:

          1. Wait for ``_active_event`` (mic on).
          2. Open WS + send config + drain via gather(sender, receiver).
          3. On any close (normal OR exception) loop back to step 1 —
             ``_active_event`` may already have been cleared by a
             concurrent ``pause()``, which short-circuits the next
             connect attempt without burning the backoff.

        State transitions emitted via ``_set_state``; never via raw
        ``_emit("ws_status", …)`` so the in-memory ``_connection_state``
        and the wire stay in lockstep.
        """
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover — present via uv.lock
            logger.error("[%s] websockets package not available: %s", self.LOG_TAG, exc)
            self._set_state(STTConnectionState.ERROR, detail=f"websockets missing: {exc}")
            return

        self._loop = asyncio.get_running_loop()
        self._audio_queue = asyncio.Queue()
        self._active_event = asyncio.Event()  # Starts CLEARED — resume() flips it on.

        backoff = BACKOFF_INITIAL

        while not self._closed:
            # ── Step 1: wait for mic-on (resume()) ──
            if not self._active_event.is_set():
                # Only emit IDLE if we're transitioning back to it after a
                # connected session (so the chip flips off promptly on
                # pause). Initial boot already sits at IDLE.
                self._set_state(STTConnectionState.IDLE, detail="paused")
                await self._active_event.wait()
                if self._closed:
                    return
                # Reset failure counter on a fresh resume — the user just
                # turned the mic on, treat this as a clean start.
                self._consecutive_failures = 0
                backoff = BACKOFF_INITIAL

            # ── Step 2: connect ──
            attempt_label = self._consecutive_failures + 1
            self._set_state(
                STTConnectionState.RECONNECTING if attempt_label > 1
                else STTConnectionState.CONNECTING,
                attempt=attempt_label,
                detail=f"attempt {attempt_label}",
            )

            try:
                async with websockets.connect(
                    self.WS_URL,
                    max_size=None,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    self._current_ws_holder["ws"] = ws
                    await ws.send(json.dumps(self._build_config_message()))
                    self._on_connected()
                    self._connection_count += 1
                    self._consecutive_failures = 0
                    backoff = BACKOFF_INITIAL
                    self._set_state(
                        STTConnectionState.CONNECTED,
                        attempt=0,
                        detail=f"connected (cycle #{self._connection_count})",
                    )
                    logger.info(
                        "[%s] WS connected (cycle #%d)",
                        self.LOG_TAG, self._connection_count,
                    )

                    # Drain audio + inbound until WS closes or an exception.
                    # Use FIRST_COMPLETED + cancel pending so the sender
                    # doesn't hang on ``audio_queue.get()`` after the
                    # receiver returns on a server / pause-initiated WS
                    # close. ``gather`` would block forever in that case.
                    sender_t = asyncio.create_task(self._sender(ws))
                    receiver_t = asyncio.create_task(self._receiver(ws))
                    try:
                        done, pending = await asyncio.wait(
                            {sender_t, receiver_t},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        # Propagate any exception from the completed task
                        # so the outer ``except Exception`` path runs the
                        # reconnect / ERROR flow.
                        for t in done:
                            exc = t.exception()
                            if exc is not None:
                                raise exc
                    finally:
                        for t in (sender_t, receiver_t):
                            if not t.done():
                                t.cancel()
                                try:
                                    await t
                                except (asyncio.CancelledError, Exception):
                                    pass

                # gather returned normally → server closed (idle timeout
                # or pause() forced close). Outer while re-enters.
                self._current_ws_holder["ws"] = None
                if self._closed:
                    return
                if not self._active_event.is_set():
                    # pause() forced close — silent IDLE, no reconnect noise.
                    logger.info("[%s] WS closed by pause()", self.LOG_TAG)
                    continue
                # Server-initiated close while still active → reconnect.
                logger.info(
                    "[%s] WS closed by server; reconnecting in %.1fs",
                    self.LOG_TAG, backoff,
                )

            except asyncio.CancelledError:
                self._current_ws_holder["ws"] = None
                raise
            except Exception as exc:
                self._current_ws_holder["ws"] = None
                if self._closed:
                    return
                self._consecutive_failures += 1
                logger.exception(
                    "[%s] WS error (failure #%d); reconnecting in %.1fs",
                    self.LOG_TAG, self._consecutive_failures, backoff,
                )
                if self._consecutive_failures >= ERROR_CONSECUTIVE_FAILURES:
                    # Terminal — surface to the route so it can rebuild.
                    self._set_state(
                        STTConnectionState.ERROR,
                        attempt=self._consecutive_failures,
                        detail=f"{self._consecutive_failures} consecutive failures: {exc}",
                    )
                    # Continue trying anyway; if the network comes back
                    # we want to recover even without a route rebuild.

            # ── Common backoff ──
            if not self._closed and self._active_event.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _sender(self, ws) -> None:
        assert self._audio_queue is not None
        while True:
            chunk = await self._audio_queue.get()
            if self._closed or chunk == b"":
                try:
                    await self._send_end_of_audio(ws)
                except Exception:
                    pass
                return
            # Skip audio when paused — the WS is being torn down, no point
            # sending bytes that will fail mid-flight.
            if self._active_event is not None and not self._active_event.is_set():
                continue
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


async def _close_ws_quietly(ws) -> None:
    """Close a WS, swallowing any exception. Used by ``pause()`` which
    can't await but needs the live socket gone so ``_receiver``'s
    async-for unblocks.
    """
    try:
        await ws.close()
    except Exception:
        pass
