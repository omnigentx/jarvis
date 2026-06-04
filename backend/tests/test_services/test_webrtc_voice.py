"""WebRTC voice bridge — in-process aiortc loopback.

Two RTCPeerConnections in one process (a synthetic "browser" + the real
``WebRtcVoiceSession``) negotiate over localhost and exchange audio, proving the
plumbing end-to-end WITHOUT a browser:

  * inbound: the browser's mic track → resample 48k→16k → feed_audio callback
  * outbound: server-pushed 24k TTS PCM → 48k track → frames at the browser

This verifies signalling (offer/answer), track wiring, and both resample paths.
It does NOT (cannot, headless) verify audio fidelity or on-device AEC — those
are manual tests.
"""
from __future__ import annotations

import asyncio
import time
from fractions import Fraction

import av
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription

from services.webrtc_voice import WebRtcVoiceSession


class _ToneTrack(MediaStreamTrack):
    """Synthetic 48 kHz mono mic source — non-silent so resampling yields data."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._ts: int | None = None
        self._start = 0.0

    async def recv(self) -> av.AudioFrame:
        if self._ts is None:
            self._start = time.time()
            self._ts = 0
        else:
            self._ts += 960
            delay = self._start + self._ts / 48000 - time.time()
            if delay > 0:
                await asyncio.sleep(delay)
        frame = av.AudioFrame(format="s16", layout="mono", samples=960)
        frame.sample_rate = 48000
        frame.pts = self._ts
        frame.time_base = Fraction(1, 48000)
        frame.planes[0].update(b"\x20\x00" * 960)
        return frame


async def _run_loopback():
    fed: list[bytes] = []
    # No STUN — host-only candidates keep the loopback fast and offline.
    session = WebRtcVoiceSession(lambda pcm: fed.append(pcm), ice_servers=[])

    browser = RTCPeerConnection()
    browser.addTrack(_ToneTrack())
    out_frames: list[int] = []

    @browser.on("track")
    def _on_track(track: MediaStreamTrack) -> None:  # noqa: ANN202
        async def pull() -> None:
            try:
                for _ in range(25):
                    fr = await track.recv()
                    out_frames.append(fr.samples)
            except Exception:
                pass
        asyncio.ensure_future(pull())

    await browser.setLocalDescription(await browser.createOffer())
    answer = await session.handle_offer(browser.localDescription.sdp, browser.localDescription.type)
    await browser.setRemoteDescription(RTCSessionDescription(**answer))

    # Stream some TTS PCM (24 kHz mono s16, 20 ms = 480 samples) out the track.
    for _ in range(10):
        session.send_pcm(b"\x15\x00" * 480)

    # Wait for ICE/DTLS + a little media to flow (localhost loopback is fast).
    for _ in range(50):
        if fed and out_frames:
            break
        await asyncio.sleep(0.1)

    await session.close()
    await browser.close()
    return fed, out_frames


def test_webrtc_loopback_bridges_audio_both_directions():
    fed, out_frames = asyncio.run(_run_loopback())
    # Inbound: the browser's mic reached feed_audio as 16 kHz mono s16 (even byte
    # count per chunk = valid int16 PCM).
    assert fed, "inbound mic never reached feed_audio"
    assert all(len(p) % 2 == 0 for p in fed), "feed_audio got odd-length (non-s16) PCM"
    # Outbound: the server's TTS track produced frames the browser received.
    assert out_frames, "outbound TTS track produced no frames at the peer"
    assert all(n > 0 for n in out_frames)
