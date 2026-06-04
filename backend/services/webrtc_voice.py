"""WebRTC voice transport — audio over an RTCPeerConnection.

WHY: browser acoustic echo cancellation (AEC) only scrubs the bot's TTS out of
the mic when playback is a WebRTC media track rendered via ``<audio>``. iOS
Safari does NOT run AEC for Web Audio API (AudioContext/AudioWorklet) playback,
so the WS+AudioWorklet path echoes the bot into STT → feedback loop on iPhone.
Routing the *audio* through WebRTC fixes that on every browser.

SCOPE: only the media (mic in, TTS out) moves to WebRTC. Signaling (SDP/ICE)
and every control/STT/barge-in event stay on ``/ws/voice`` — this module is a
self-contained audio bridge the WS handler drives. It exposes:

    * ``TtsAudioTrack``  — outbound: server ``push(pcm_24k)`` → 48 kHz frames.
    * ``WebRtcVoiceSession`` — wraps RTCPeerConnection, answers an offer, pumps
      the inbound mic track into a ``feed_audio(pcm_16k)`` callback, and owns the
      outbound TtsAudioTrack.

Rates: STT wants 16 kHz mono s16 (``feed_audio``); RealtimeTTS/Edge emit 24 kHz
mono s16; WebRTC/Opus is 48 kHz. av.AudioResampler bridges them.

The audio PLUMBING here is covered by an in-process aiortc loopback test
(tests/test_services/test_webrtc_voice.py). Audio *fidelity* (pitch/latency) and
the actual on-device AEC are validated by manual testing — they can't be
asserted headless.
"""
from __future__ import annotations

import asyncio
import fractions
import logging
import os
import time
from typing import Callable, Optional

import av
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError

logger = logging.getLogger(__name__)

# Public STUN so each peer can discover its server-reflexive (NAT) candidate —
# needed for the browser↔server connection to form over the internet (iPhone on
# cellular/Wi-Fi behind NAT). If a deployment is behind symmetric NAT on both
# ends a TURN relay is also required; configure via JARVIS_WEBRTC_ICE as a
# comma-separated list, e.g.:
#   JARVIS_WEBRTC_ICE="stun:stun.l.google.com:19302,turn:user:pass@turn.example.com:3478"
_DEFAULT_STUN = "stun:stun.l.google.com:19302"


def parse_ice_servers() -> list[dict]:
    """Parse JARVIS_WEBRTC_ICE into browser-shaped iceServers dicts.

    Accepts ``stun:host:port`` and ``turn[s]:user:pass@host:port`` entries.
    Returned shape ({urls, username?, credential?}) is what an RTCConfiguration
    expects in the browser AND maps cleanly to aiortc's RTCIceServer — so the
    frontend (via the /ice endpoint) and the server use one source of truth.
    """
    raw = os.environ.get("JARVIS_WEBRTC_ICE", _DEFAULT_STUN)
    servers: list[dict] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "@" in entry:
            scheme_creds, host = entry.split("@", 1)
            parts = scheme_creds.split(":")  # e.g. ["turn", "user", "pass"]
            server: dict = {"urls": f"{parts[0]}:{host}"}
            if len(parts) > 1:
                server["username"] = parts[1]
            if len(parts) > 2:
                server["credential"] = ":".join(parts[2:])  # pass may contain ':'
            servers.append(server)
        else:
            servers.append({"urls": entry})
    return servers

STT_RATE = 16000      # feed_audio() input rate (mono s16)
TTS_RATE = 24000      # RealtimeTTS / Edge PCM rate (mono s16)
WEBRTC_RATE = 48000   # Opus / WebRTC standard
FRAME_SAMPLES = 960   # 20 ms @ 48 kHz — one outbound frame
_SILENCE_48K = b"\x00\x00" * FRAME_SAMPLES

FeedAudio = Callable[[bytes], None]


class TtsAudioTrack(MediaStreamTrack):
    """Outbound audio: server pushes 24 kHz TTS PCM; we emit paced 48 kHz frames.

    Emits silence when idle so the track stays live between replies (a dead
    track would force renegotiation on the next reply). ``recv()`` paces frames
    to real time using aiortc's documented timestamp/sleep pattern.
    """

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._queue: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=WEBRTC_RATE)
        self._buf = bytearray()           # resampled 48 kHz mono s16 awaiting framing
        self._timestamp: Optional[int] = None
        self._start = 0.0

    def push(self, pcm24k: bytes) -> None:
        """Queue a chunk of 24 kHz mono s16 PCM for playback."""
        if pcm24k:
            self._queue.put_nowait(pcm24k)

    def pending(self) -> bool:
        """True while there's still TTS audio queued or buffered to play."""
        return not self._queue.empty() or len(self._buf) >= 2

    async def wait_drained(self) -> None:
        """Block until every pushed chunk has been emitted as a frame.

        With WebRTC the server paces playback, so this is the authoritative
        'the user has stopped hearing the bot' edge — the WebRTC analogue of the
        WS path's client ``playback_done``.
        """
        while self.pending():
            await asyncio.sleep(0.02)

    def flush(self) -> None:
        """Drop all queued + buffered TTS audio (barge-in — stop playback now)."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._buf.clear()

    def _resample_into_buf(self, pcm24k: bytes) -> None:
        in_frame = av.AudioFrame(format="s16", layout="mono", samples=len(pcm24k) // 2)
        in_frame.sample_rate = TTS_RATE
        in_frame.pts = None
        in_frame.planes[0].update(pcm24k)
        for out in self._resampler.resample(in_frame):
            # out.samples*2 bytes of real audio; planes[0] may be padded, so slice.
            self._buf.extend(bytes(out.planes[0])[: out.samples * 2])

    async def _next_frame_bytes(self) -> bytes:
        # Top up the buffer until we have a full 20 ms frame, but don't block the
        # cadence: if no TTS arrives within one frame interval, emit silence.
        while len(self._buf) < FRAME_SAMPLES * 2:
            try:
                pcm24k = await asyncio.wait_for(self._queue.get(), timeout=FRAME_SAMPLES / WEBRTC_RATE)
            except asyncio.TimeoutError:
                return _SILENCE_48K
            self._resample_into_buf(pcm24k)
        out = bytes(self._buf[: FRAME_SAMPLES * 2])
        del self._buf[: FRAME_SAMPLES * 2]
        return out

    async def recv(self) -> av.AudioFrame:
        if self._timestamp is None:
            self._start = time.time()
            self._timestamp = 0
        else:
            self._timestamp += FRAME_SAMPLES
            target = self._start + self._timestamp / WEBRTC_RATE
            delay = target - time.time()
            if delay > 0:
                await asyncio.sleep(delay)

        data = await self._next_frame_bytes()
        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.sample_rate = WEBRTC_RATE
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, WEBRTC_RATE)
        frame.planes[0].update(data)
        return frame


async def pump_inbound_track(track: MediaStreamTrack, feed_audio: FeedAudio) -> None:
    """Read the peer's mic track, resample each frame to 16 kHz mono s16, feed STT.

    Runs until the track ends (peer left / connection closed). Resampling is
    per-frame; aiortc decodes Opus to PCM frames before we see them.
    """
    resampler = av.AudioResampler(format="s16", layout="mono", rate=STT_RATE)
    try:
        while True:
            frame = await track.recv()
            for out in resampler.resample(frame):
                pcm = bytes(out.planes[0])[: out.samples * 2]
                if pcm:
                    feed_audio(pcm)
    except MediaStreamError:
        return  # track ended — normal teardown
    except Exception:
        logger.exception("[webrtc] inbound pump crashed")


class WebRtcVoiceSession:
    """One browser ↔ server audio session over WebRTC.

    The WS handler creates this on a ``webrtc_offer`` control frame, calls
    ``handle_offer`` to get the answer SDP (sent back over the WS), then uses
    ``send_pcm`` to stream TTS audio. Inbound mic audio is forwarded to the
    ``feed_audio`` callback (the same one the WS binary path used).
    """

    def __init__(
        self,
        feed_audio: FeedAudio,
        *,
        on_connection_lost: Optional[Callable[[], None]] = None,
        ice_servers: Optional[list[str]] = None,
    ):
        self._feed_audio = feed_audio
        self._on_connection_lost = on_connection_lost
        # Default to ICE from env (prod NAT traversal, incl. TURN creds); tests
        # pass ice_servers=[] for a fast host-only localhost loopback.
        if ice_servers is not None:
            ice = [RTCIceServer(urls=u) for u in ice_servers]
        else:
            ice = [
                RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
                for s in parse_ice_servers()
            ]
        self.pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice))
        self.tts_track = TtsAudioTrack()
        self._inbound_task: Optional[asyncio.Task] = None
        self.pc.addTrack(self.tts_track)

        @self.pc.on("track")
        def _on_track(track: MediaStreamTrack) -> None:  # noqa: ANN202
            if track.kind != "audio":
                return
            logger.info("[webrtc] inbound %s track", track.kind)
            self._inbound_task = asyncio.ensure_future(pump_inbound_track(track, self._feed_audio))

        @self.pc.on("connectionstatechange")
        async def _on_state() -> None:  # noqa: ANN202
            logger.info("[webrtc] connection state: %s", self.pc.connectionState)
            if self.pc.connectionState in ("failed", "closed", "disconnected"):
                if self._on_connection_lost is not None:
                    self._on_connection_lost()

    async def handle_offer(self, sdp: str, sdp_type: str = "offer") -> dict[str, str]:
        """Apply the browser's offer, return the answer as ``{sdp, type}``."""
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        return {"sdp": self.pc.localDescription.sdp, "type": self.pc.localDescription.type}

    def send_pcm(self, pcm24k: bytes) -> None:
        """Push a chunk of 24 kHz mono s16 TTS PCM to the outbound track."""
        self.tts_track.push(pcm24k)

    async def close(self) -> None:
        if self._inbound_task is not None:
            self._inbound_task.cancel()
        try:
            await self.pc.close()
        except Exception:
            logger.exception("[webrtc] close failed")
