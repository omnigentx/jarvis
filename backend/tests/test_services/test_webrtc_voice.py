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

import httpx
import pytest

from services import webrtc_voice
from services.webrtc_voice import WebRtcVoiceSession, get_ice_servers, parse_ice_servers


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


def test_parse_ice_servers_stun_and_turn(monkeypatch):
    """JARVIS_WEBRTC_ICE → browser-shaped iceServers, incl. TURN credentials.

    The /api/voice/ice endpoint + the aiortc session both consume this, so a
    mis-parse (esp. TURN user:pass) would silently break NAT traversal on prod.
    """
    monkeypatch.setenv(
        "JARVIS_WEBRTC_ICE",
        "stun:stun.l.google.com:19302, turn:alice:s3cr3t@turn.example.com:3478",
    )
    servers = parse_ice_servers()
    assert servers[0] == {"urls": "stun:stun.l.google.com:19302"}
    assert servers[1] == {
        "urls": "turn:turn.example.com:3478",
        "username": "alice",
        "credential": "s3cr3t",
    }

    # Default (env unset) is a public STUN server, never empty.
    monkeypatch.delenv("JARVIS_WEBRTC_ICE", raising=False)
    assert parse_ice_servers() == [{"urls": "stun:stun.l.google.com:19302"}]

    # A colon in the TURN password survives.
    monkeypatch.setenv("JARVIS_WEBRTC_ICE", "turn:u:p:a:ss@h:3478")
    assert parse_ice_servers()[0]["credential"] == "p:a:ss"


# ─── Cloudflare TURN minting (get_ice_servers) ──────────────────────────────

_CF_RESPONSE = {
    "iceServers": [
        {"urls": ["stun:stun.cloudflare.com:3478"]},
        {
            "urls": [
                "turn:turn.cloudflare.com:3478?transport=udp",
                "turns:turn.cloudflare.com:5349?transport=tcp",
            ],
            "username": "minted-user",
            "credential": "minted-pass",
        },
    ]
}


@pytest.fixture
def _cf_env(monkeypatch):
    """CF TURN env set + module cache reset (it's process-global).

    DB secrets are stubbed empty so these tests exercise the env fallback
    deterministically (no config DB involvement)."""
    monkeypatch.setenv("JARVIS_CF_TURN_KEY_ID", "key123")
    monkeypatch.setenv("JARVIS_CF_TURN_API_TOKEN", "tok456")
    monkeypatch.setattr(webrtc_voice, "_db_turn_secrets", lambda: {})
    monkeypatch.setitem(webrtc_voice._cf_cache, "servers", None)
    monkeypatch.setitem(webrtc_voice._cf_cache, "minted_at", 0.0)


class _FakeResp:
    def __init__(self, payload, status=201):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload


def test_get_ice_servers_without_cf_env_falls_back_to_parse(monkeypatch):
    monkeypatch.delenv("JARVIS_CF_TURN_KEY_ID", raising=False)
    monkeypatch.delenv("JARVIS_CF_TURN_API_TOKEN", raising=False)
    monkeypatch.delenv("JARVIS_WEBRTC_ICE", raising=False)
    monkeypatch.setattr(webrtc_voice, "_db_turn_secrets", lambda: {})
    assert get_ice_servers() == [{"urls": "stun:stun.l.google.com:19302"}]


def test_db_secrets_take_priority_over_env(monkeypatch):
    """Settings/Wizard-stored creds (DB) are authoritative; env is bootstrap
    fallback only. A divergence must resolve to the DB value."""
    monkeypatch.setenv("JARVIS_CF_TURN_KEY_ID", "env-key")
    monkeypatch.setenv("JARVIS_CF_TURN_API_TOKEN", "env-tok")
    monkeypatch.setattr(
        webrtc_voice, "_db_turn_secrets",
        lambda: {"key_id": "db-key", "api_token": "db-tok"},
    )
    monkeypatch.setitem(webrtc_voice._cf_cache, "servers", None)
    monkeypatch.setitem(webrtc_voice._cf_cache, "minted_at", 0.0)

    seen: list[tuple[str, str]] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.append((url, headers["Authorization"]))
        return _FakeResp(_CF_RESPONSE)

    monkeypatch.setattr(httpx, "post", fake_post)
    get_ice_servers()
    assert seen[0][0].endswith("/turn/keys/db-key/credentials/generate-ice-servers")
    assert seen[0][1] == "Bearer db-tok"


def test_invalidate_turn_cache_forces_remint(_cf_env, monkeypatch):
    """Saving new TURN secrets in Settings fires the config listener →
    invalidate_turn_cache() → next call re-mints instead of serving cache."""
    calls: list[int] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (calls.append(1), _FakeResp(_CF_RESPONSE))[1])

    get_ice_servers()
    get_ice_servers()
    assert len(calls) == 1  # cache hit
    webrtc_voice.invalidate_turn_cache()
    get_ice_servers()
    assert len(calls) == 2


def test_get_ice_servers_mints_from_cloudflare_and_caches(_cf_env, monkeypatch):
    calls: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        return _FakeResp(_CF_RESPONSE)

    monkeypatch.setattr(httpx, "post", fake_post)

    servers = get_ice_servers()
    assert servers == _CF_RESPONSE["iceServers"]
    assert calls[0]["url"].endswith("/turn/keys/key123/credentials/generate-ice-servers")
    assert calls[0]["headers"]["Authorization"] == "Bearer tok456"
    assert calls[0]["json"] == {"ttl": webrtc_voice._CF_TURN_TTL}

    # Second call inside the refresh window: served from cache, no new mint.
    assert get_ice_servers() == _CF_RESPONSE["iceServers"]
    assert len(calls) == 1


def test_mint_url_percent_encodes_key_id(_cf_env, monkeypatch):
    """A mistyped key_id with a '/' must not silently hit a different API path."""
    monkeypatch.setenv("JARVIS_CF_TURN_KEY_ID", "bad/key id")
    calls: list[str] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        return _FakeResp(_CF_RESPONSE)

    monkeypatch.setattr(httpx, "post", fake_post)
    get_ice_servers()
    assert calls[0].endswith("/turn/keys/bad%2Fkey%20id/credentials/generate-ice-servers")


def test_get_ice_servers_refreshes_after_half_life(_cf_env, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (calls.append(1), _FakeResp(_CF_RESPONSE))[1])

    get_ice_servers()
    # Age the cache past the refresh threshold → next call re-mints.
    webrtc_voice._cf_cache["minted_at"] = (
        time.monotonic() - webrtc_voice._CF_REFRESH_AFTER - 1
    )
    get_ice_servers()
    assert len(calls) == 2


def test_get_ice_servers_serves_stale_when_mint_fails(_cf_env, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp(_CF_RESPONSE))
    assert get_ice_servers() == _CF_RESPONSE["iceServers"]

    # Past refresh threshold but still inside hard expiry; CF API now erroring.
    webrtc_voice._cf_cache["minted_at"] = (
        time.monotonic() - webrtc_voice._CF_REFRESH_AFTER - 1
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp({}, status=500))
    assert get_ice_servers() == _CF_RESPONSE["iceServers"], "stale-but-valid creds must keep voice alive"

    # Past hard expiry with the API still down → env/STUN fallback, never stale creds.
    monkeypatch.delenv("JARVIS_WEBRTC_ICE", raising=False)
    webrtc_voice._cf_cache["minted_at"] = (
        time.monotonic() - webrtc_voice._CF_HARD_EXPIRY - 1
    )
    assert get_ice_servers() == [{"urls": "stun:stun.l.google.com:19302"}]


def test_session_accepts_browser_shaped_ice_dicts():
    """ws_voice passes get_ice_servers() output (dicts with creds + url LISTS)
    straight into the session — a shape regression here breaks prod silently."""
    async def build_and_close():
        session = WebRtcVoiceSession(
            lambda pcm: None,
            ice_servers=[
                {"urls": ["stun:stun.cloudflare.com:3478"]},
                {
                    "urls": ["turn:turn.cloudflare.com:3478?transport=udp"],
                    "username": "u",
                    "credential": "c",
                },
                "stun:stun.l.google.com:19302",  # legacy plain-string entry
            ],
        )
        await session.close()

    asyncio.run(build_and_close())


def test_webrtc_loopback_bridges_audio_both_directions():
    fed, out_frames = asyncio.run(_run_loopback())
    # Inbound: the browser's mic reached feed_audio as 16 kHz mono s16 (even byte
    # count per chunk = valid int16 PCM).
    assert fed, "inbound mic never reached feed_audio"
    assert all(len(p) % 2 == 0 for p in fed), "feed_audio got odd-length (non-s16) PCM"
    # Outbound: the server's TTS track produced frames the browser received.
    assert out_frames, "outbound TTS track produced no frames at the peer"
    assert all(n > 0 for n in out_frames)


def test_tts_track_paces_backlog_instead_of_bursting():
    """Pacing debt must NEVER be repaid as a faster-than-real-time burst.

    When recv() falls behind wall clock (idle-frame drift or an event-loop
    stall), the old code emitted the entire backlog instantly once TTS data
    arrived; the browser's jitter buffer treats those frames as late and
    discards them — heard on prod (iPhone/5G) as the reply's first seconds
    missing. The fix re-anchors the pacing clock instead, so the first
    second of a reply must take ~1 s of wall time to emit.
    """
    async def run() -> float:
        track = webrtc_voice.TtsAudioTrack()
        for _ in range(5):  # prime the pacing clock
            await track.recv()
        await asyncio.sleep(1.0)  # starve recv() → 1 s of pacing debt
        for _ in range(20):  # a 2 s reply, pushed in 100 ms chunks
            track.push(b"\x11\x00" * 2400)
        t0 = time.time()
        emitted = 0
        while emitted < webrtc_voice.WEBRTC_RATE:  # pull 1.0 s of media
            await track.recv()
            emitted += webrtc_voice.FRAME_SAMPLES
        return time.time() - t0

    elapsed = asyncio.run(run())
    assert elapsed >= 0.8, (
        f"first 1.0s of reply emitted in {elapsed:.3f}s wall — catch-up burst; "
        "the receiver's jitter buffer will drop the head of the reply"
    )
    assert elapsed <= 1.5, f"pacing too slow: {elapsed:.3f}s wall for 1.0s of media"


def test_tts_track_flushes_partial_tail_so_wait_drained_completes():
    """A reply whose PCM isn't 20 ms-frame-aligned must still drain: the
    partial tail is flushed padded with silence, otherwise pending() stays
    True forever and speak()'s wait_drained() hangs bot_speaking high."""
    async def run() -> None:
        track = webrtc_voice.TtsAudioTrack()
        track.push(b"\x11\x00" * 720)  # 30 ms @ 24 kHz → 1.5 outbound frames
        await track.recv()             # full frame
        await track.recv()             # padded tail flush
        assert not track.pending(), "partial tail left pending() stuck True"
        await asyncio.wait_for(track.wait_drained(), timeout=1.0)

    asyncio.run(run())
