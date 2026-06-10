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
