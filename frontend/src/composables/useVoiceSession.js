/**
 * useVoiceSession — manages the hands-free /ws/voice round-trip.
 *
 * Lifecycle:
 *   start() →
 *     1. open WebSocket to /ws/voice (?api_key=...)
 *     2. getUserMedia({ audio: { channelCount: 1 } })
 *     3. register the pcm16-resampler AudioWorklet, pipe mic → worklet
 *     4. forward each Int16 chunk over the socket as a binary frame
 *   stop()  → tears down both sides cleanly
 *   speak(text) → ask the server to TTS-stream a reply right now
 *   bargeIn() → cancel any in-flight TTS so the user can talk over it
 *
 * Server events (JSON text frames) populate reactive state so the UI can
 * render live — partial transcripts, VAD, wake-word, TTS markers.
 *
 * Server audio (binary frames) is decoded as MP3 chunks (when chat engine
 * is Edge — the legacy provider returns MP3) OR raw PCM (24 kHz int16
 * mono — when chat engine is RealtimeTTS-backed). We branch on the magic
 * bytes since the protocol is the same socket either way.
 */
import { ref, reactive, shallowRef, onScopeDispose } from 'vue'
import { useChatStore } from '../stores/chat.js'
import { useAudioPlayerStore } from '../stores/audioPlayer.js'
import { EVENTS, on } from '../auth/bus.js'
import { expandToolRequest, expandToolDone } from '../utils/toolEvents.js'
import { createPlaybackDoneTracker } from './playbackDoneTracker.js'

const PCM_PLAYBACK_RATE = 24000  // RealtimeTTS engines emit 24 kHz mono

// ── Singleton ─────────────────────────────────────────────────────────────
// Voice session must persist across route nav so the user can keep talking
// while looking at /monitor, and so the global hands-free indicator (in
// AppLayout) shares state with VoiceBar (in /chat). Without this, every
// call to ``useVoiceSession()`` would create independent state and the
// indicator would always show "Off" even when the mic was actually live.
let _singleton = null

export function useVoiceSession() {
  if (_singleton) return _singleton
  _singleton = _createVoiceSession()
  return _singleton
}

function _createVoiceSession() {
  const chatStore = useChatStore()
  const status = ref('idle')  // 'idle' | 'connecting' | 'loading_stt' | 'listening' | 'thinking' | 'speaking' | 'error'
  // wsStatus mirrors the upstream STT WebSocket lifecycle (Soniox, Deepgram,
  // …) — distinct from ``status`` which tracks the FRONTEND↔BACKEND ws_voice
  // socket. Frontend chip renders off this so a green pill always means
  // "STT really is connected and audio is reaching it", not "frontend has
  // a WS to the backend". See [[ws lifecycle fix 2026-05-29]].
  //
  // Values match ``STTConnectionState`` in
  // backend/services/stt_backends/types.py:
  //   'idle' | 'connecting' | 'connected' | 'reconnecting' | 'error'
  const wsStatus = ref('idle')
  const error = ref('')
  const partialTranscript = ref('')
  const lastFinalTranscript = ref('')
  const isUserSpeaking = ref(false)        // recording_start → recording_stop window
  const wasInterrupted = ref(false)         // last reply was cancelled by barge-in
  const sessionId = ref('')
  const wakeWordHits = ref(0)
  const events = reactive([])  // last N events for debug overlay
  // Currently-streaming assistant message id in chatStore. Set when we
  // create the placeholder bubble; finalised on assistant_message; marked
  // as errored on tts_interruption / error.
  let pendingAgentMsgId = null
  // The id of the last user message we pushed via voice that hasn't
  // received an ``assistant_message`` reply yet. Tracked SEPARATELY
  // from ``pendingAgentMsgId`` because ``tts_interruption`` can clear
  // the placeholder before the next ``user_message`` lands, so we
  // can't use the placeholder's existence as the "previous turn was
  // unanswered" signal. Cleared when a real assistant reply arrives.
  let lastVoiceUserMsgId = null
  // Set to ``true`` the moment the user (or unmount hook) initiates
  // teardown. Browsers don't drop the WS instantly — frames already in
  // flight from the server can fire ``onmessage`` while the socket is
  // in CLOSING state, and without this guard the late ``user_message``
  // / ``agent_thinking`` would re-add a chat bubble + placeholder *after*
  // we already showed the "Off" pill (the screenshot-33 ghost message
  // bug). Reset on each successful ``start()``.
  let torn = false

  // Barge-in SSoT: the server keeps ``bot_speaking`` True until WE report that
  // the buffered TTS audio has finished playing. The tracker decides when —
  // after tts_end AND the playback queue drains — and we do the actual WS send.
  // (Timing logic lives in playbackDoneTracker.js so it's unit-testable.)
  const playbackTracker = createPlaybackDoneTracker(() => {
    if (ws.value?.readyState === 1) {
      ws.value.send(JSON.stringify({ type: 'playback_done' }))
    }
  })

  const ws = shallowRef(null)
  const stream = shallowRef(null)
  const audioContext = shallowRef(null)
  const workletNode = shallowRef(null)
  const playbackContext = shallowRef(null)
  const playbackTime = ref(0)
  // Track every BufferSourceNode currently scheduled so a barge-in or
  // explicit Interrupt can stop the queued audio mid-playback. Without
  // this, chunks that were already ``src.start()``-ed would keep playing
  // after the server cancelled TTS, and the user would hear several
  // more seconds of bot voice after they tried to talk over it (the
  // "TTS doesn't stop when user barges in" bug).
  const playbackSources = new Set()

  // WebRTC audio transport (preferred). Routing mic + TTS over an
  // RTCPeerConnection lets the browser's AEC scrub the bot's voice out of the
  // mic — the only reliable fix for the iOS Safari echo/feedback loop (Web Audio
  // API playback bypasses AEC on Safari; a WebRTC <audio> track does not). When
  // active, the WS audio paths (worklet mic-send + _enqueueAudio) are bypassed;
  // we fall back to them only if RTCPeerConnection is unavailable.
  const pc = shallowRef(null)
  const remoteAudioEl = shallowRef(null)
  let webrtcMode = false

  function _ensureActiveConversation() {
    if (chatStore.activeConversation) return
    // First voice turn with no chat session active — create a local conv.
    // When the backend session event lands, finalizeAgentMessage's
    // meta.conversation_id swap reconciles the local id to the backend id.
    if (typeof chatStore.createConversation === 'function') {
      chatStore.createConversation(chatStore.activeAgentName || null)
    }
  }

  function _dropPending() {
    // Silently drop the streaming placeholder. Used when the turn was
    // cancelled in a way the user already understands (they barged in,
    // they hit Stop, the agent had nothing to say). No "(interrupted)" /
    // "(no response)" bubble — the chat thread stays clean.
    if (pendingAgentMsgId && typeof chatStore.removeMessage === 'function') {
      try { chatStore.removeMessage(pendingAgentMsgId) } catch {}
    }
    pendingAgentMsgId = null
  }

  function _failPending(detail) {
    // Surface a real error the user should see (network drop, agent runtime
    // unavailable). Different from _dropPending — this leaves a visible
    // error bubble in red so the failure is debuggable.
    if (pendingAgentMsgId && typeof chatStore.setMessageError === 'function') {
      try { chatStore.setMessageError(pendingAgentMsgId, detail) } catch {}
    }
    pendingAgentMsgId = null
  }

  function _flushPlaybackQueue() {
    // A barge-in already told the server to stop (it lowers bot_speaking on
    // the cancel), so suppress the drain-triggered playback_done that the
    // src.stop() ``onended`` callbacks below would otherwise fire.
    playbackTracker.flush()
    for (const src of playbackSources) {
      try { src.stop() } catch {}
    }
    playbackSources.clear()
    if (playbackContext.value) {
      // Reset the cursor so the next TTS turn schedules from "now" rather
      // than the dangling end-time of the cancelled stream.
      playbackTime.value = playbackContext.value.currentTime
    }
  }

  function _logEvent(name, payload) {
    events.unshift({ ts: Date.now(), name, payload })
    if (events.length > 30) events.pop()
  }

  function _onMessage(ev) {
    // Late events during socket CLOSING / after stop() must not mutate
    // chat state. Without this the next mounted VoiceBar sees stale
    // placeholders + user messages it has no way to clear.
    if (torn) {
      if (typeof ev.data === 'string') {
        try {
          const m = JSON.parse(ev.data)
          console.debug('[voice] dropping post-teardown event', m.type)
        } catch {}
      }
      return
    }
    if (typeof ev.data === 'string') {
      let msg
      try { msg = JSON.parse(ev.data) } catch { return }
      // Diagnostic so manual voice-flow debugging shows the actual
      // event sequence + state transitions in browser devtools.
      // Cheap (just a console.log per event) and quiet enough at
      // info level to leave on by default.
      console.debug(
        '[voice] event %s | status=%s pending=%s lastUserMsg=%s | payload=%o',
        msg.type, status.value, pendingAgentMsgId, lastVoiceUserMsgId, msg,
      )
      _logEvent(msg.type, msg)
      switch (msg.type) {
        case 'stt_loading': status.value = 'loading_stt'; break
        case 'stt_ready':   status.value = 'listening'; break
        case 'partial_transcript':
        case 'stable_transcript':
          partialTranscript.value = msg.text || ''
          break
        case 'final_transcript':
          lastFinalTranscript.value = msg.text || ''
          partialTranscript.value = ''
          break
        case 'recording_start': isUserSpeaking.value = true; break
        case 'recording_stop':  isUserSpeaking.value = false; break
        case 'ws_status':
          // Upstream STT WS state (idle/connecting/connected/reconnecting/error).
          // Drives the WS chip in the chat header; does NOT replace the
          // existing ``status`` ref which tracks the frontend↔backend WS.
          wsStatus.value = msg.state || 'idle'
          break

        case 'user_message': {
          // Push the user turn into chatStore so it appears in the main
          // chat panel — same UI as typed messages, no separate voice log.
          _ensureActiveConversation()
          if (typeof chatStore.addUserMessage === 'function' && msg.text) {
            // STT-correction coalesce: a fresh user_message arriving while
            // the previous turn is still in the agent_thinking phase
            // (pendingAgentMsgId set, no assistant_message yet) means the
            // backend already cancelled that turn via _cancel_inflight on
            // recording_start. Leaving the previous user bubble + spinner
            // in place would surface the half-utterance the user is
            // correcting — drop them so only the latest stands.
            if (pendingAgentMsgId) {
              try { chatStore.removeMessage?.(pendingAgentMsgId) } catch {}
              pendingAgentMsgId = null
              const conv = chatStore.activeConversation
              if (conv) {
                for (let i = conv.messages.length - 1; i >= 0; i--) {
                  if (conv.messages[i].role === 'user') {
                    try { chatStore.removeMessage?.(conv.messages[i].id) } catch {}
                    break
                  }
                }
              }
            }
            chatStore.addUserMessage(msg.text)
          }
          // Voice flow guarantees agent_thinking follows user_message —
          // flip status here too so the badge transitions instantly even
          // if the agent_thinking event is delayed by a few ms (avoids
          // the "is it processing?" confusion the user reported).
          status.value = 'thinking'
          break
        }
        case 'agent_thinking': {
          // Add a streaming placeholder so the user sees an obvious
          // "thinking…" bubble while the LLM generates.
          _ensureActiveConversation()
          if (typeof chatStore.addAgentMessagePlaceholder === 'function') {
            pendingAgentMsgId = chatStore.addAgentMessagePlaceholder()
          }
          status.value = 'thinking'
          break
        }
        case 'tool_request':
          // Mirror ChatView.vue's text-chat handling so the same compact
          // "X tools used" bubble renders identically for voice turns.
          // Falls through quietly if the placeholder hasn't been created
          // yet (pushToolCall does its own existence check).
          if (pendingAgentMsgId && typeof chatStore.pushToolCall === 'function') {
            for (const payload of expandToolRequest(msg)) {
              chatStore.pushToolCall(pendingAgentMsgId, payload)
            }
          }
          break
        case 'tool_done':
          if (pendingAgentMsgId && typeof chatStore.pushToolCall === 'function') {
            for (const payload of expandToolDone(msg)) {
              chatStore.pushToolCall(pendingAgentMsgId, payload)
            }
          }
          break
        case 'tool_running':
          // Lifecycle ping ("X is now running tool Y") — already covered
          // by the tool_request bubble; intentionally not re-rendered.
          break
        case 'assistant_message': {
          const text = msg.text || ''
          const meta = msg.session_id ? { conversation_id: msg.session_id } : {}
          if (msg.empty) {
            // Empty reply — drop the placeholder rather than leaving a
            // "(no response)" bubble; the thread stays clean. Status reset
            // explicitly because no TTS chunks will arrive to do it for us.
            _dropPending()
            wasInterrupted.value = false
            if (status.value === 'thinking') {
              status.value = ws.value?.readyState === 1 ? 'listening' : 'idle'
            }
            break
          }
          if (pendingAgentMsgId && typeof chatStore.finalizeAgentMessage === 'function') {
            chatStore.finalizeAgentMessage(pendingAgentMsgId, text, meta)
          } else {
            // Defensive: missing thinking placeholder — synthesise one then finalize.
            _ensureActiveConversation()
            const id = chatStore.addAgentMessagePlaceholder?.()
            if (id) chatStore.finalizeAgentMessage?.(id, text, meta)
          }
          // Story playback by voice routes through the SAME singleton player as
          // typed chat (mini-player, chapter nav, resume). The story plays via
          // the audio player, NOT the voice TTS channel — the backend already
          // suppressed speaking the announcement. Pass ttsEnabled=true because
          // the user explicitly asked to listen (voice has no read-aloud toggle).
          if (msg.story) {
            useAudioPlayerStore().playFromChat({ story: msg.story }, true)
          }
          pendingAgentMsgId = null
          wasInterrupted.value = false
          // Status will flip to 'speaking' when the next tts_start arrives;
          // in the rare case the agent reply produces no TTS chunks we still
          // want to recover, so leave 'thinking' here and rely on tts_end /
          // tts_interruption to flip back.
          break
        }
        case 'session':
          // Backend session id; finalizeAgentMessage swaps the local conv
          // id to this on its meta.conversation_id path. Just track for UI.
          sessionId.value = msg.id || ''
          break

        case 'vad_start':
        case 'vad_stop':
          // VAD events are debug-only — recording_start is the canonical
          // turn boundary signal we already handle above.
          break
        case 'wake_word': wakeWordHits.value++; break
        case 'webrtc_answer':
          // Server accepted the WebRTC offer — complete the handshake; the
          // connection forms once ICE finishes and audio starts flowing on the
          // media track (mic out, TTS in).
          if (pc.value && msg.sdp) {
            pc.value
              .setRemoteDescription({ type: msg.sdp_type || 'answer', sdp: msg.sdp })
              .catch((e) => console.warn('[voice] setRemoteDescription failed', e))
          }
          break
        case 'webrtc_error':
          // Server couldn't set up WebRTC — fail loud, no fallback. The user
          // can re-toggle the mic; this is usually a server-side misconfig.
          console.warn('[voice] server webrtc_error', msg.detail)
          error.value = `Voice transport failed on the server: ${msg.detail || 'unknown error'}`
          status.value = 'error'
          break
        case 'tts_start':
          status.value = 'speaking'
          wasInterrupted.value = false
          playbackTracker.ttsStart()   // re-arm playback_done for this turn
          break
        case 'tts_end':
          // Server finished synthesising. Playback usually continues a few more
          // seconds; the tracker fires playback_done once the queue drains (or
          // right now if a very short reply already drained before tts_end).
          playbackTracker.ttsEnd(playbackSources.size)
          status.value = ws.value?.readyState === 1 ? 'listening' : 'idle'
          break
        case 'barge_in_ack':
          // Server confirms it stopped synthesising. Any chunks already
          // in the playback queue must be cancelled here too — otherwise
          // the user keeps hearing the bot for several more seconds even
          // though TTS production stopped.
          _flushPlaybackQueue()
          status.value = ws.value?.readyState === 1 ? 'listening' : 'idle'
          break
        case 'tts_interruption':
          // Same as barge_in_ack: stop any audio that already shipped from
          // server before the cancellation propagated, so the user's voice
          // gets the floor instantly instead of competing with bot residue.
          _flushPlaybackQueue()
          // Distinguish Case A vs Case B by the presence of a streaming
          // placeholder:
          //   * Case A — LLM still generating: ``pendingAgentMsgId`` set,
          //     ``assistant_message`` hasn't landed yet. The placeholder
          //     represents *this* turn so we mark it INTERRUPTED in place
          //     instead of dropping it (keeps a visible "the bot was cut
          //     off here" marker rather than a phantom missing turn).
          //   * Case B — LLM finished, only TTS was cancelled: pending is
          //     null, the assistant message has already been finalised.
          //     Nothing on the chat side was interrupted — just the audio
          //     — so we leave every bubble alone.
          //
          // ``wasInterrupted`` (global ref) remains set so the user-side
          // BARGE-IN chip still lands on the most recent user message in
          // both cases (the user *did* barge in either way).
          if (pendingAgentMsgId !== null) {
            wasInterrupted.value = true
            if (typeof chatStore.markMessageInterrupted === 'function') {
              try { chatStore.markMessageInterrupted(pendingAgentMsgId) } catch {}
            }
            pendingAgentMsgId = null
          } else {
            wasInterrupted.value = true  // for user-side BARGE-IN chip only
          }
          status.value = ws.value?.readyState === 1 ? 'listening' : 'idle'
          break
        case 'error':
          _failPending(msg.detail || 'agent error')
          error.value = msg.detail || 'server error'
          // Flip status to 'error' so the header/VoiceBar stop showing a green
          // "connected" chip while STT is actually dead. Without this the chip
          // stayed green on a Soniox 408 (transient errors no longer reach here
          // — only fatal ones do — so this means STT genuinely failed). The
          // user sees the mic is off and can re-toggle to reconnect.
          status.value = 'error'
          break
      }
    } else if (!webrtcMode && ev.data instanceof Blob) {
      // WS audio path only — in WebRTC mode TTS arrives on the media track.
      _enqueueAudio(ev.data)
    } else if (!webrtcMode && ev.data instanceof ArrayBuffer) {
      _enqueueAudio(new Blob([ev.data]))
    }
  }

  async function _enqueueAudio(blob) {
    // Backend always emits int16 mono PCM at PCM_PLAYBACK_RATE on /ws/voice
    // — RealtimeTTS engines via stream_pcm() and EdgeTTSProvider via the
    // server-side ffmpeg MP3→PCM pipe. Per-chunk decodeAudioData on partial
    // MP3 frames was the source of the previous "broken audio / stutter"
    // glitches, so we removed the format sniff and treat all binary
    // frames as raw PCM. AudioBuffers are scheduled back-to-back via a
    // running playbackTime cursor so chunks play seamlessly.
    if (!playbackContext.value) {
      playbackContext.value = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: PCM_PLAYBACK_RATE,
      })
      playbackTime.value = playbackContext.value.currentTime
    }
    const ctx = playbackContext.value
    try {
      const buf = await blob.arrayBuffer()
      // Defensive: int16 needs an even byte count; truncate odd byte at tail
      // (rare, but a partially-filled chunk shouldn't crash).
      const usable = buf.byteLength - (buf.byteLength % 2)
      if (usable <= 0) return
      const samples = new Int16Array(buf, 0, usable / 2)
      const audioBuffer = ctx.createBuffer(1, samples.length, PCM_PLAYBACK_RATE)
      const channel = audioBuffer.getChannelData(0)
      for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 0x8000
      const src = ctx.createBufferSource()
      src.buffer = audioBuffer
      src.connect(ctx.destination)
      // Schedule strictly after the previous chunk so timing is glitch-free
      // even if WS frames arrive in bursts (a small lookahead vs ctx.currentTime
      // also smooths over jittery delivery).
      const startAt = Math.max(ctx.currentTime + 0.02, playbackTime.value)
      src.start(startAt)
      // Track for barge-in flush; auto-untrack when the chunk finishes
      // playing on its own to keep the set bounded.
      playbackSources.add(src)
      src.onended = () => {
        playbackSources.delete(src)
        // When the LAST buffered chunk finishes (and the server already sent
        // tts_end), the user has stopped hearing the bot → report playback_done.
        playbackTracker.chunkEnded(playbackSources.size)
      }
      playbackTime.value = startAt + audioBuffer.duration
    } catch (e) {
      console.warn('[voice] audio enqueue failed', e)
    }
  }

  const _webrtcSupported = () =>
    typeof RTCPeerConnection !== 'undefined' && typeof MediaStream !== 'undefined'

  // Wait for ICE gathering to finish (non-trickle) so the offer SDP carries all
  // candidates — simplest signalling over our WS. Cap the wait so a browser that
  // never reports 'complete' still sends the offer with whatever it gathered.
  function _iceGatheringComplete(peer, timeoutMs = 2500) {
    if (peer.iceGatheringState === 'complete') return Promise.resolve()
    return new Promise((resolve) => {
      const done = () => { peer.removeEventListener('icegatheringstatechange', check); resolve() }
      const check = () => { if (peer.iceGatheringState === 'complete') done() }
      peer.addEventListener('icegatheringstatechange', check)
      setTimeout(done, timeoutMs)
    })
  }

  /**
   * Negotiate WebRTC audio over the already-open WS. Mic goes out as a track;
   * the bot's TTS comes back as a track we play through a hidden <audio
   * playsinline> — which is what engages the browser AEC (incl. iOS Safari).
   * On any failure we leave webrtcMode false so start() uses the WS audio path.
   */
  async function _startWebRtc(sock, ms) {
    const peer = new RTCPeerConnection({
      iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
    })
    pc.value = peer

    const audioEl = document.createElement('audio')
    audioEl.autoplay = true
    audioEl.setAttribute('playsinline', '')   // iOS: play inline, not fullscreen
    audioEl.style.display = 'none'
    document.body.appendChild(audioEl)
    remoteAudioEl.value = audioEl
    peer.addEventListener('track', (ev) => {
      audioEl.srcObject = ev.streams[0] || new MediaStream([ev.track])
      // play() may reject if autoplay is gated; the mic click is a user gesture
      // so it normally succeeds — swallow the rejection either way.
      audioEl.play?.().catch(() => {})
    })
    peer.addEventListener('connectionstatechange', () => {
      const st = peer.connectionState
      console.debug('[voice] webrtc connectionState', st)
      // Fail loud: if the peer connection can't form (ICE/DTLS failed), surface
      // it rather than sitting silently with no audio. No fallback — the user
      // re-toggles, or sets a TURN server (see docs/VOICE_WEBRTC_TESTING.md).
      if (st === 'failed') {
        error.value = 'Voice connection failed — WebRTC could not connect (NAT/firewall?).'
        status.value = 'error'
      }
    })

    for (const track of ms.getAudioTracks()) peer.addTrack(track, ms)

    await peer.setLocalDescription(await peer.createOffer())
    await _iceGatheringComplete(peer)
    sock.send(JSON.stringify({
      type: 'webrtc_offer',
      sdp: peer.localDescription.sdp,
      sdp_type: peer.localDescription.type,
    }))
    webrtcMode = true
  }

  function _teardownWebRtc() {
    webrtcMode = false
    try { pc.value?.close() } catch {}
    pc.value = null
    const el = remoteAudioEl.value
    if (el) {
      try { el.srcObject = null; el.remove() } catch {}
    }
    remoteAudioEl.value = null
  }

  function _wsUrl() {
    // Auth rides on the ``jarvis_session`` cookie; browsers attach it to
    // the WebSocket upgrade handshake automatically when the URL is
    // same-origin. The backend's _ws_authenticated() reads the cookie
    // header first (see backend/routes/ws_voice.py). No api_key in URL.
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${proto}//${location.host}/ws/voice`
  }

  async function start() {
    // Allow (re)start from idle OR error — a failed session must be
    // recoverable by re-toggling the mic. Any other state (connecting/
    // listening/…) means a session is already live, so guard against re-entry.
    if (status.value !== 'idle' && status.value !== 'error') return
    // Clean up any half-torn-down previous session before opening a new one,
    // so an errored session doesn't leak its old socket/mic/worklet.
    if (status.value === 'error') await stop()
    error.value = ''
    status.value = 'connecting'
    torn = false
    try {
      const sock = new WebSocket(_wsUrl())
      sock.binaryType = 'arraybuffer'
      ws.value = sock
      await new Promise((resolve, reject) => {
        sock.onopen = resolve
        sock.onerror = (e) => reject(new Error('WebSocket failed to open'))
      })
      sock.onmessage = _onMessage
      sock.onclose = (ev) => {
        // Diagnostic: code 1000 = clean close (user/server stop),
        // 1001 = going away (page nav / browser closing tab),
        // 1006 = abnormal closure (no close frame — network drop).
        // Helps distinguish "user clicked Stop" from "connection
        // dropped" when an unexpected Mic-Off shows up.
        console.debug('[voice] ws.onclose', {
          code: ev?.code, reason: ev?.reason, wasClean: ev?.wasClean,
          torn, status: status.value,
        })
        // Differentiate clean close (user clicked Stop — drop silently) vs
        // unexpected drop (show error). status==='error' means onerror
        // already surfaced something; otherwise treat as clean close.
        if (status.value === 'error') {
          _failPending('(connection closed)')
        } else {
          _dropPending()
        }
        if (status.value !== 'error') status.value = 'idle'
        isUserSpeaking.value = false
      }
      sock.onerror = () => {
        if (status.value !== 'error') {
          status.value = 'error'
          error.value = 'WebSocket error'
        }
      }

      // echoCancellation MUST be on — without it, the bot's TTS playback
      // leaks back into the mic and STT transcribes its own voice as a new
      // user turn (we observed Whisper hallucinating Chinese characters
      // from a VN "Xin chào" greeting being echoed back). noiseSuppression stays off
      // because aggressive WebRTC NS strips actual speech as "background"
      // and tanks RMS to the 20–100 range. autoGainControl stays off to
      // let the user's hardware level pass through unscaled.
      const ms = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: false,
          autoGainControl: false,
        },
      })
      stream.value = ms
      // Log what the browser actually applied — getUserMedia constraints
      // are *requests*; the browser is allowed to downgrade or ignore them
      // depending on device support. Reading this back via getSettings is
      // the only way to confirm AEC is actually in the capture path.
      try {
        const track = ms.getAudioTracks()[0]
        const settings = track?.getSettings?.() || {}
        const caps = track?.getCapabilities?.() || {}
        console.log('[voice diag] track settings', settings)
        console.log('[voice diag] track capabilities', caps)
        sock.send(JSON.stringify({
          type: 'diag',
          stage: 'mic_track',
          settings,
        }))
      } catch (e) {
        console.warn('[voice diag] track inspect failed', e)
      }

      // Audio transport. WebRTC is REQUIRED — it's the only path with browser
      // AEC (the iOS echo fix). We do NOT silently fall back to the WS audio
      // path on failure: a broken transport must surface loudly, not degrade to
      // the echo-prone path behind the user's back. The legacy WS path is only
      // reachable by an EXPLICIT opt-in (localStorage.voice_transport='ws') for
      // debugging / A-B testing — a deliberate choice, not a fallback.
      let _forceWs = false
      try { _forceWs = localStorage.getItem('voice_transport') === 'ws' } catch { /* ignore */ }
      if (_forceWs) {
        // Explicit WS audio path: AudioWorklet captures mic → 16 kHz PCM over
        // the WS; TTS arrives as binary frames (_enqueueAudio).
        const ac = new (window.AudioContext || window.webkitAudioContext)()
        console.log('[voice diag] AudioContext sampleRate', ac.sampleRate)
        try {
          sock.send(JSON.stringify({ type: 'diag', stage: 'audio_context', sampleRate: ac.sampleRate }))
        } catch (e) { /* noop */ }
        audioContext.value = ac
        await ac.audioWorklet.addModule('/voice-worklet.js')
        const node = new AudioWorkletNode(ac, 'pcm16-resampler', { processorOptions: { targetRate: 16000, frameMs: 100 } })
        workletNode.value = node
        const micSource = ac.createMediaStreamSource(ms)
        micSource.connect(node)
        // Don't connect to destination — we don't want the user to hear themselves.
        node.port.onmessage = (e) => {
          if (sock.readyState === 1) sock.send(e.data)
        }
      } else {
        if (!_webrtcSupported()) {
          // Fail loud rather than degrade — every target browser supports WebRTC.
          throw new Error('This browser does not support WebRTC, which voice mode requires.')
        }
        // Throws on setup failure → caught by start()'s handler → status 'error'.
        await _startWebRtc(sock, ms)
      }
      sock.send(JSON.stringify({ type: 'start' }))
      // Tell the server which agent + conversation the dashboard has
      // selected so voice turns route through the same agent the user is
      // chatting with in the text UI (default Jarvis if unset).
      if (chatStore.activeAgentName) {
        sock.send(JSON.stringify({ type: 'set_agent', name: chatStore.activeAgentName }))
      }
      if (chatStore.activeConversationId) {
        sock.send(JSON.stringify({ type: 'set_session', id: chatStore.activeConversationId }))
      }
      status.value = 'listening'
    } catch (e) {
      error.value = e?.message || String(e)
      status.value = 'error'
      await stop()
    }
  }

  async function stop() {
    // Diagnostic — capture the call site so the next "Mic Off
    // mystery" report has evidence (was it the user button? an
    // unmount? Vite HMR? something else?). Cheap and dev-only useful;
    // production builds keep it for parity in case we need to repro
    // a bug in deployed UI.
    try {
      console.debug(
        '[voice] stop() called',
        { status: status.value, hmr: !!import.meta.hot },
        new Error('voice-stop-trace').stack,
      )
    } catch {}
    // Mark torn-down FIRST so any in-flight WS event arriving while
    // close() propagates is ignored by ``_onMessage``. Then drop the
    // placeholder so ``stop()`` followed by a stray late event can't
    // re-add a "(voice session closed)" ghost bubble.
    torn = true
    _dropPending()
    if (ws.value) {
      // Detach the message handler so even if the browser dispatches a
      // queued frame after close(), nothing happens. ``torn`` is the
      // belt; nulling onmessage is the suspenders.
      try { ws.value.onmessage = null } catch {}
      try { ws.value.send(JSON.stringify({ type: 'stop' })) } catch {}
      try { ws.value.close() } catch {}
      ws.value = null
    }
    workletNode.value?.disconnect()
    workletNode.value = null
    _teardownWebRtc()
    if (stream.value) {
      stream.value.getTracks().forEach(t => t.stop())
      stream.value = null
    }
    if (audioContext.value) {
      try { await audioContext.value.close() } catch {}
      audioContext.value = null
    }
    status.value = 'idle'
    // The backend would emit ws_status: idle inside its pause() flow, but
    // the WS is already torn down by the time we run — guarantee chip
    // flips off immediately rather than waiting for a frame that won't
    // arrive.
    wsStatus.value = 'idle'
    isUserSpeaking.value = false
    partialTranscript.value = ''
  }

  function speak(text) {
    if (ws.value?.readyState !== 1) return
    ws.value.send(JSON.stringify({ type: 'speak', text }))
  }

  function bargeIn() {
    if (ws.value?.readyState !== 1) return
    ws.value.send(JSON.stringify({ type: 'barge_in' }))
  }

  // Auth expiry: voice sessions are long-lived WebSockets that won't
  // notice a key rotation until the user speaks again. Tear down on
  // EXPIRED so the AuthGate doesn't have a ghost-streaming session
  // behind it. Registered at module-singleton init time — never
  // unsubscribed (singleton lives for the page lifetime).
  on(EVENTS.EXPIRED, () => {
    if (status.value !== 'idle') {
      stop().catch(() => { /* already torn */ })
    }
  })

  return {
    status, wsStatus, error,
    partialTranscript, lastFinalTranscript,
    isUserSpeaking, wasInterrupted, sessionId,
    wakeWordHits, events,
    start, stop, speak, bargeIn,
  }
}
