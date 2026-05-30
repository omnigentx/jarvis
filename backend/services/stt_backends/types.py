"""Provider-agnostic STT service contract.

Defines the surface that ``routes/ws_voice.py`` and ``services/runtime_config.py``
depend on, so adding a new STT backend (Deepgram, AssemblyAI, Vosk…) is a
single-file change in ``services/stt_backends/<name>.py`` — no churn in the
voice WS route, the runtime swap path, or the frontend chip rendering.

Existing backends that satisfy this protocol:

* :class:`services.stt_backends._ws_streaming.WSStreamingSTT` (base for cloud
  WS providers — Soniox today)
* :class:`services.stt_realtime.RealtimeSTTService` (local faster-whisper
  wrapper, always-ready, ``pause``/``resume`` are no-ops)
* :class:`services.stt_backends.gipformer_vi.GipformerSTTService` (local
  sherpa-onnx, same always-ready shape)

The connection-state machine + ``ws_status`` event vocabulary belong here
(not in any concrete backend) because the frontend chip must render the same
way regardless of which provider drives it.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable


class STTConnectionState(str, Enum):
    """Provider-agnostic readiness state. Frontend mirrors this 1:1.

    Transition graph (only the listed edges are valid):

    ::

        IDLE ──resume()──> CONNECTING ──ack──> CONNECTED ─┐
         ▲                     │                          │
         │                     │ error                    │ server close /
         │                     ▼                          │ network blip
         │              RECONNECTING <─────────────────────┘
         │                     │
         │  pause() / shutdown │ backoff cap exceeded
         └─────────────────────┴──> ERROR (terminal — caller should rebuild)

    Local backends (faster-whisper, sherpa) skip CONNECTING/RECONNECTING and
    jump straight to CONNECTED after model init — they have no socket to
    drop. The state machine is identical; CONNECTING is just instantaneous.
    """

    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


#: Hook callable forwarded by the route. Receives ``(event_name, payload)``.
EventHook = Callable[[str, dict[str, Any]], None]


#: Canonical event vocabulary every backend MUST use via the hook. Adding a
#: new event requires updating this list + the route's forwarder + the
#: frontend ``useVoiceSession`` switch — keeping the wire schema tight.
KNOWN_EVENTS = frozenset({
    # Connection lifecycle (NEW with the state-machine refactor)
    "ws_status",          # payload: {"state": STTConnectionState, "attempt": int, "detail": str}
    # Per-utterance
    "recording_start",    # user opened mouth
    "recording_stop",     # user closed mouth (utterance done)
    "vad_stop",           # VAD detected silence (often coincides with recording_stop)
    "partial_transcript", # payload: {"text": str}
    "final_transcript",   # payload: {"text": str}
    # Errors during streaming (not connection-level — those go via ws_status=ERROR)
    "error",              # payload: {"detail": str}
})


@runtime_checkable
class STTServiceProtocol(Protocol):
    """Contract every STT backend implements. ``ws_voice`` depends on THIS
    surface only — never on a concrete class.

    Conformance check lives in
    ``tests/test_stt_protocol_conformance.py``; adding a backend without
    one of these methods fails CI loudly.
    """

    # ── Audio I/O ──
    def feed_audio(self, pcm_chunk: bytes) -> None:
        """Push 16 kHz mono int16 PCM. Non-blocking. Safe to call from any
        thread (must internally hand off to the service's own loop)."""
        ...

    # ── Hook ──
    def set_hook(self, hook: EventHook | None) -> None:
        """Install / clear the event hook. Last writer wins."""
        ...

    # ── Lifecycle ──
    def start_listen_loop(self) -> None:
        """Boot the background worker thread (if any). Idempotent.

        Called once at service construction. Distinct from ``resume`` —
        ``start_listen_loop`` initialises the long-running infrastructure
        (asyncio loop, model worker), while ``resume`` flips the "active"
        flag that controls whether audio is actually streamed/processed.
        """
        ...

    def resume(self) -> None:
        """Activate the provider: open the cloud WS / allow audio through
        the worker. Drives IDLE → CONNECTING → CONNECTED.

        Idempotent. For local providers may be a no-op (always ready).
        Called by ``ws_voice`` when the frontend WS connects (= mic on).
        """
        ...

    def pause(self) -> None:
        """Deactivate: close the cloud WS / suspend audio processing.
        Drives any state → IDLE.

        Idempotent. ``resume`` after ``pause`` MUST be cheap (< 2s) — the
        whole point is that a hot-reuse beats teardown+rebuild on every
        page reload. Called by ``ws_voice`` when the frontend WS closes.
        """
        ...

    def shutdown(self) -> None:
        """Permanently destroy. Cannot be resumed. Called when the
        backend itself is being swapped out (settings change) or the
        backend process is shutting down."""
        ...

    # ── State ──
    @property
    def is_alive(self) -> bool:
        """True if the service CAN still produce transcripts (worker
        thread alive, no fatal init failure). False = caller MUST
        rebuild via ``runtime_config.apply_voice_stt_config``.

        Distinct from ``connection_state`` — a CLOUD backend in
        RECONNECTING is still "alive" (the worker thread is up, trying);
        ``is_alive`` only goes False on terminal failure or after
        ``shutdown()``.
        """
        ...

    @property
    def connection_state(self) -> STTConnectionState:
        """Current state for UI. Backends emit ``ws_status`` events on
        every transition; this property is the source of truth for any
        late subscriber (e.g. frontend reload mid-session)."""
        ...
