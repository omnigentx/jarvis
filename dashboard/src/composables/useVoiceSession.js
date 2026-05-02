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
import { ref, reactive, shallowRef } from 'vue'
import { getApiKey } from '../api.js'

const PCM_PLAYBACK_RATE = 24000  // RealtimeTTS engines emit 24 kHz mono

export function useVoiceSession() {
  const status = ref('idle')  // 'idle' | 'connecting' | 'listening' | 'speaking' | 'error'
  const error = ref('')
  const partialTranscript = ref('')
  const lastFinalTranscript = ref('')
  const wakeWordHits = ref(0)
  const events = reactive([])  // last N events for debug overlay

  const ws = shallowRef(null)
  const stream = shallowRef(null)
  const audioContext = shallowRef(null)
  const workletNode = shallowRef(null)
  const playbackContext = shallowRef(null)
  const playbackTime = ref(0)

  function _logEvent(name, payload) {
    events.unshift({ ts: Date.now(), name, payload })
    if (events.length > 30) events.pop()
  }

  function _onMessage(ev) {
    if (typeof ev.data === 'string') {
      let msg
      try { msg = JSON.parse(ev.data) } catch { return }
      _logEvent(msg.type, msg)
      switch (msg.type) {
        case 'partial_transcript': partialTranscript.value = msg.text || ''; break
        case 'stable_transcript':  partialTranscript.value = msg.text || ''; break
        case 'final_transcript':
          lastFinalTranscript.value = msg.text || ''
          partialTranscript.value = ''
          break
        case 'vad_start': status.value = 'listening'; break
        case 'vad_stop':  /* keep status — listening continues */ break
        case 'wake_word': wakeWordHits.value++; break
        case 'tts_start': status.value = 'speaking'; break
        case 'tts_end':
        case 'barge_in_ack':
          status.value = ws.value?.readyState === 1 ? 'listening' : 'idle'
          break
        case 'error': error.value = msg.detail || 'server error'; break
      }
    } else if (ev.data instanceof Blob) {
      _enqueueAudio(ev.data)
    } else if (ev.data instanceof ArrayBuffer) {
      _enqueueAudio(new Blob([ev.data]))
    }
  }

  async function _enqueueAudio(blob) {
    if (!playbackContext.value) {
      playbackContext.value = new (window.AudioContext || window.webkitAudioContext)()
      playbackTime.value = playbackContext.value.currentTime
    }
    const ctx = playbackContext.value
    const buf = await blob.arrayBuffer()
    // Sniff: MP3 starts with 0xFF 0xFB / 0xFF 0xF3 / 0xFF 0xF2 / "ID3"
    // Anything else we treat as raw int16 mono PCM at PCM_PLAYBACK_RATE.
    const header = new Uint8Array(buf, 0, Math.min(3, buf.byteLength))
    const looksLikeMp3 = header[0] === 0xff || (header[0] === 0x49 && header[1] === 0x44 && header[2] === 0x33)
    try {
      let audioBuffer
      if (looksLikeMp3) {
        audioBuffer = await ctx.decodeAudioData(buf.slice(0))
      } else {
        const samples = new Int16Array(buf)
        audioBuffer = ctx.createBuffer(1, samples.length, PCM_PLAYBACK_RATE)
        const channel = audioBuffer.getChannelData(0)
        for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 0x8000
      }
      const src = ctx.createBufferSource()
      src.buffer = audioBuffer
      src.connect(ctx.destination)
      const startAt = Math.max(ctx.currentTime, playbackTime.value)
      src.start(startAt)
      playbackTime.value = startAt + audioBuffer.duration
    } catch (e) {
      console.warn('[voice] audio decode failed', e)
    }
  }

  function _wsUrl() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const apiKey = getApiKey()
    const params = apiKey ? `?api_key=${encodeURIComponent(apiKey)}` : ''
    return `${proto}//${location.host}/ws/voice${params}`
  }

  async function start() {
    if (status.value !== 'idle') return
    error.value = ''
    status.value = 'connecting'
    try {
      const sock = new WebSocket(_wsUrl())
      sock.binaryType = 'arraybuffer'
      ws.value = sock
      await new Promise((resolve, reject) => {
        sock.onopen = resolve
        sock.onerror = (e) => reject(new Error('WebSocket failed to open'))
      })
      sock.onmessage = _onMessage
      sock.onclose = () => { status.value = 'idle' }

      const ms = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } })
      stream.value = ms

      const ac = new (window.AudioContext || window.webkitAudioContext)()
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
      sock.send(JSON.stringify({ type: 'start' }))
      status.value = 'listening'
    } catch (e) {
      error.value = e?.message || String(e)
      status.value = 'error'
      await stop()
    }
  }

  async function stop() {
    try { ws.value?.send(JSON.stringify({ type: 'stop' })) } catch {}
    try { ws.value?.close() } catch {}
    ws.value = null
    workletNode.value?.disconnect()
    workletNode.value = null
    if (stream.value) {
      stream.value.getTracks().forEach(t => t.stop())
      stream.value = null
    }
    if (audioContext.value) {
      try { await audioContext.value.close() } catch {}
      audioContext.value = null
    }
    status.value = 'idle'
  }

  function speak(text) {
    if (ws.value?.readyState !== 1) return
    ws.value.send(JSON.stringify({ type: 'speak', text }))
  }

  function bargeIn() {
    if (ws.value?.readyState !== 1) return
    ws.value.send(JSON.stringify({ type: 'barge_in' }))
  }

  return {
    status, error, partialTranscript, lastFinalTranscript, wakeWordHits, events,
    start, stop, speak, bargeIn,
  }
}
