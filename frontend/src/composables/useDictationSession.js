/**
 * useDictationSession — press-to-talk dictation over /ws/voice.
 *
 * Why a separate composable instead of reusing useVoiceSession:
 * the hands-free composable runs a full conversation pipeline (transcript →
 * LLM → TTS → audio playback). Dictation only wants the transcript; the
 * server short-circuits LLM/TTS when we send ``mode: "dictation"`` in the
 * start handshake. Keeping the two as separate composables means the
 * dictation path never touches chatStore (no ghost message bubbles, no
 * placeholder reconciliation, no playback queue) and the user can run
 * dictation from ChatInput while the singleton hands-free session in
 * VoiceBar is idle. Trying to overload the conversation singleton with a
 * "ignore the bot half" flag would scatter conditionals across both
 * codepaths — cheaper to keep them disjoint.
 *
 * Lifecycle (mirrors useVoiceSession's first half, intentionally; the
 * worklet contract is identical so the same /voice-worklet.js script
 * works for both):
 *   start() → open WS, send {type:'start', mode:'dictation'} → request
 *             mic → wire the pcm16 worklet → forward PCM frames
 *   stop()  → close the socket and tear down audio
 *
 * Reactive state the caller binds to:
 *   status:   'idle' | 'connecting' | 'loading_stt' | 'listening' | 'error'
 *   partial:  the live, not-yet-final transcript fragment (provisional)
 *   final:    accumulated finalised utterances since the last reset()
 *   error:    last error string, '' when none
 *
 * The caller decides how to merge partial + final into a UI field — for
 * the chat composer that's "previousText + finals + partial" with the
 * partial styled as live tail so the user sees what's still landing.
 */
import { ref, shallowRef, onScopeDispose } from 'vue'

export function useDictationSession() {
  const status = ref('idle')
  const error = ref('')
  // ``partial`` is whatever Soniox/faster-whisper hasn't finalised yet —
  // overwritten on every event (NOT appended) because providers re-emit
  // the running provisional from scratch each frame.
  const partial = ref('')
  // ``final`` accumulates every final_transcript since the last reset().
  // Endpoint detection (Soniox <end>) flushes provisional into a final,
  // so this grows utterance-by-utterance until the user clicks Stop.
  const final = ref('')

  const ws = shallowRef(null)
  const stream = shallowRef(null)
  const audioContext = shallowRef(null)
  const workletNode = shallowRef(null)

  function reset() {
    partial.value = ''
    final.value = ''
  }

  function _wsUrl() {
    // Same auth contract as the hands-free socket — cookie attaches on
    // upgrade, so no api_key in the URL. Different path component is
    // unnecessary; the server flips behaviour based on the start payload.
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${proto}//${location.host}/ws/voice`
  }

  function _onMessage(ev) {
    if (typeof ev.data !== 'string') {
      // Server may still send TTS audio frames if a misconfigured backend
      // ignores ``mode: dictation``; we just drop them here so the user
      // never hears a stray reply during dictation.
      return
    }
    let msg
    try { msg = JSON.parse(ev.data) } catch { return }
    switch (msg.type) {
      case 'stt_loading':
        status.value = 'loading_stt'
        break
      case 'stt_ready':
        status.value = 'listening'
        break
      case 'partial_transcript':
      case 'stable_transcript':
        partial.value = msg.text || ''
        break
      case 'final_transcript': {
        const text = (msg.text || '').trim()
        if (text) {
          // Preserve the user's typing spacing: append with a single space
          // between utterances. The provider strips its own trailing space,
          // so we add one here unconditionally; ChatInput trims at submit.
          final.value = final.value ? `${final.value} ${text}` : text
        }
        partial.value = ''
        break
      }
      case 'error':
        error.value = msg.detail || 'server error'
        status.value = 'error'
        break
      // user_message / agent_thinking / assistant_message / tts_* should
      // not arrive in dictation mode; if a server bug emits them anyway we
      // ignore them rather than leak into the chat thread.
    }
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
        sock.onerror = () => reject(new Error('WebSocket failed to open'))
      })
      sock.onmessage = _onMessage
      sock.onclose = () => {
        if (status.value !== 'error') status.value = 'idle'
      }
      sock.onerror = () => {
        if (status.value !== 'error') {
          status.value = 'error'
          error.value = 'WebSocket error'
        }
      }

      // Same constraints as the hands-free path: AEC on (otherwise STT
      // would transcribe room echo as input on devices with poor mic
      // isolation), NS/AGC off (NS strips quiet speech, AGC fights the
      // user's hardware gain).
      const ms = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: false,
          autoGainControl: false,
        },
      })
      stream.value = ms

      const ac = new (window.AudioContext || window.webkitAudioContext)()
      audioContext.value = ac
      await ac.audioWorklet.addModule('/voice-worklet.js')
      const node = new AudioWorkletNode(ac, 'pcm16-resampler', {
        processorOptions: { targetRate: 16000, frameMs: 100 },
      })
      workletNode.value = node
      const micSource = ac.createMediaStreamSource(ms)
      micSource.connect(node)
      // Intentionally NOT connecting node → destination: we don't want
      // the user to hear themselves through the speakers.
      node.port.onmessage = (e) => {
        if (sock.readyState === 1) sock.send(e.data)
      }

      // The mode flag is the whole reason this composable exists — the
      // backend ws_voice route reads it and bypasses LLM/TTS dispatch on
      // every final_transcript.
      sock.send(JSON.stringify({ type: 'start', mode: 'dictation' }))
      status.value = 'listening'
    } catch (e) {
      error.value = e?.message || String(e)
      status.value = 'error'
      await stop()
    }
  }

  async function stop() {
    if (ws.value) {
      try { ws.value.onmessage = null } catch {}
      try { ws.value.send(JSON.stringify({ type: 'stop' })) } catch {}
      try { ws.value.close() } catch {}
      ws.value = null
    }
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
    // Leave ``partial`` and ``final`` populated — the caller (ChatInput)
    // wants to keep the dictated text in the input box for editing. Use
    // reset() explicitly to clear after the user sends or cancels.
    status.value = 'idle'
  }

  // Tear down on component unmount so a stale socket can't keep the mic
  // hot after the user navigates away.
  onScopeDispose(() => {
    if (status.value !== 'idle') stop().catch(() => {})
  })

  return { status, error, partial, final, start, stop, reset }
}
