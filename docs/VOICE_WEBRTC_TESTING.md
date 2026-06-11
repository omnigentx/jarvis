# Voice WebRTC — manual test guide

The hands-free voice path now runs **audio over WebRTC** (mic + TTS on an
`RTCPeerConnection`) instead of WebSocket + Web Audio. This is the fix for the
**iOS Safari echo/feedback loop**: Safari only runs acoustic echo cancellation
(AEC) for audio played through a WebRTC `<audio>` track, never for Web Audio API
playback. Control/STT/barge-in events still ride `/ws/voice`.

Automated coverage (all green, headless):
- backend bridge loopback — `backend/tests/test_services/test_webrtc_voice.py`
- TTS track pacing (no catch-up burst → no dropped reply head) —
  `test_webrtc_voice.py::test_tts_track_paces_backlog_instead_of_bursting`
- signalling over the WS handler — `test_ws_voice.py::test_webrtc_offer_is_answered_over_ws`
- mid-session re-offer (ICE-death recovery, server half) —
  `test_ws_voice.py::test_webrtc_reoffer_replaces_session_over_same_ws`
- barge-in SSoT — `test_ws_voice.py` (onset / playback-tail / playback_done)
- frontend negotiation + ICE-failure reconnect policy —
  `frontend/tests/e2e/flows/voice-webrtc.spec.ts`,
  `frontend/src/composables/webrtcRecovery.test.js`

What automation **cannot** check (needs a real device): audio fidelity and
whether the browser AEC actually cancels the echo. That's this guide.

---

## 1. Test on **Mac Chrome / localhost first** (no deploy needed)

localhost is a secure context, so WebRTC works fully. This validates ~95% of the
path before you ever touch prod.

```bash
./dev.sh            # http://localhost:3000
```

1. Open `http://localhost:3000` in Chrome, log in.
2. Open DevTools → Console.
3. Click the **hands-free mic** in the VoiceBar (top of chat). Allow the mic.
4. **Confirm WebRTC is in use** — in Console you should see:
   - `[voice] webrtc connectionState connecting` → `... connected`
   - no per-chunk audio logs (TTS now plays via the media track).
   - Network tab: the `/ws/voice` socket carries JSON only (a `webrtc_offer`
     out, a `webrtc_answer` back), **no binary audio frames**.
5. Say something → the agent should reply **out loud**.
6. **Barge-in:** while the bot is talking, start speaking. The TTS must stop
   **immediately** (not after you finish your sentence).

✅ If all of the above work on Chrome localhost, the plumbing is correct.

**A/B compare with the old path:** in Console run
`localStorage.voice_transport = 'ws'` then re-toggle the mic to force the legacy
WS audio path (`delete localStorage.voice_transport` to go back to WebRTC).

## 2. Test on **iPhone Safari** (needs HTTPS → prod)

iPhone can't use the Mac's localhost, and getUserMedia needs HTTPS — so deploy,
then open the prod URL in Safari on the iPhone.

1. Open the prod site in **Safari**, log in.
2. Tap the hands-free mic, allow the mic.
3. Let the bot speak, then **talk while it's speaking**.
4. **Expected:** the bot does **not** hear itself — no feedback loop, no
   self-triggered turns. Barge-in stops the bot when *you* speak.

### If it doesn't connect on prod (no audio at all)

WebRTC media is UDP and needs to traverse NAT. We send a public STUN server by
default. If the connection never reaches `connected`:

- Ensure the server can send/receive **UDP** (host firewall / security group).
- Behind a strict/symmetric NAT you may need a **TURN** relay (e.g. coturn).
  Point the backend at it:
  ```
  JARVIS_WEBRTC_ICE="stun:your.stun:3478,turn:user:pass@your.turn:3478"
  ```
  (comma-separated; the frontend also needs the matching iceServers — tell me
  and I'll wire it through.)
- Quick unblock while debugging: `localStorage.voice_transport = 'ws'` falls back
  to the old WS audio path (works, but no iOS AEC).

## What to report back

For any issue, the fastest fix comes from: the **Console logs** (esp.
`[voice] webrtc connectionState ...` and any red errors), whether it's Chrome or
Safari, and whether audio worked at all vs. only the echo/barge-in was wrong.
