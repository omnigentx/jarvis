/**
 * E2E flow: hands-free VoiceBar barge-in + playback_done SSoT.
 *
 * Guards the production contract behind the "TTS stops the instant I talk"
 * fix (cross-references backend/tests/test_routes/test_ws_voice.py and the unit
 * suite src/composables/playbackDoneTracker.test.js):
 *
 *   1. Click the VoiceBar mic → useVoiceSession.start() opens /ws/voice and
 *      sends {type:'start'}. We ack with stt_ready → status 'listening'.
 *   2. Server speaks: tts_start → PCM chunk(s) → tts_end. Once the client's
 *      playback queue drains, the client MUST send {type:'playback_done'} so
 *      the server can lower bot_speaking. This is the SSoT the barge-in checks.
 *   3. Barge-in: while audio is queued, the server sends tts_interruption. The
 *      client flushes its playback queue (status → 'listening') and must NOT
 *      send playback_done for that interrupted turn.
 *
 * Why we stub audio HW (not the barge-in logic): the real path needs a mic +
 * AudioContext we don't have in CI. getUserMedia + AudioContext + AudioWorklet
 * are stubbed to no-op, and the fake BufferSource fires ``onended`` on the next
 * macrotask so the REAL drain → playback_done logic runs deterministically.
 * The barge-in/playback logic itself is production code, unmocked.
 */

import type { Page, WebSocketRoute } from '@playwright/test'
import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

/**
 * Stub mic capture AND TTS playback. Extends the dictation spec's stub with
 * the playback half (createBuffer/createBufferSource/destination). The fake
 * BufferSource fires ``onended`` asynchronously on both start() and stop() so
 * the client's drain detection (and barge-in flush) exercise their real paths.
 */
async function stubVoiceAudio(page: Page) {
  // These specs exercise the WS audio path (playback_done / barge-in flush),
  // so opt into it explicitly. WebRTC is the default + required transport now
  // (no silent fallback); the stubbed mic here isn't a real track, so forcing
  // WS keeps this suite focused on the WS-path SSoT logic.
  await page.addInitScript(() => {
    try { localStorage.setItem('voice_transport', 'ws') } catch { /* ignore */ }
  })
  await page.addInitScript(() => {
    const fakeTrack = { stop() {}, getSettings: () => ({}), getCapabilities: () => ({}) }
    const fakeStream = { getAudioTracks: () => [fakeTrack], getTracks: () => [fakeTrack] }
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: async () => fakeStream },
    })
    class FakeAudioWorklet { async addModule() {} }
    class FakeBufferSource {
      buffer: unknown = null
      onended: (() => void) | null = null
      connect() {}
      private _fire() { setTimeout(() => { try { this.onended && this.onended() } catch {} }, 0) }
      start() { this._fire() }   // chunk "finishes playing" → drain check runs
      stop() { this._fire() }    // barge-in stop() also ends the source
    }
    class FakeAudioContext {
      audioWorklet = new FakeAudioWorklet()
      currentTime = 0
      sampleRate = 48000
      destination = {}
      createMediaStreamSource() { return { connect() {} } }
      createBuffer(_ch: number, len: number, rate: number) {
        return { duration: len / rate, getChannelData: () => new Float32Array(len) }
      }
      createBufferSource() { return new FakeBufferSource() }
      async close() {}
    }
    class FakeAudioWorkletNode { port = { onmessage: null }; connect() {} disconnect() {} }
    // @ts-expect-error — replacing globals for test
    window.AudioContext = FakeAudioContext
    // @ts-expect-error
    window.webkitAudioContext = FakeAudioContext
    // @ts-expect-error
    window.AudioWorkletNode = FakeAudioWorkletNode
  })
}

/** A minimal binary PCM frame (4 int16 samples of silence). */
const PCM_CHUNK = Buffer.alloc(8)

async function openVoiceBar(page: Page) {
  const inbound: string[] = []
  let routed: WebSocketRoute | null = null
  await page.routeWebSocket(/\/ws\/voice$/, (ws) => {
    routed = ws
    ws.onMessage((data) => {
      const s = typeof data === 'string' ? data : '<binary>'
      inbound.push(s)
      try {
        if (JSON.parse(s).type === 'start') ws.send(JSON.stringify({ type: 'stt_ready' }))
      } catch { /* binary mic frame */ }
    })
  })

  await page.goto('/chat')
  const mic = page.locator('.voice-bar .mic-btn')
  await expect(mic).toBeVisible()
  await mic.click()
  // Handshake + ack → listening.
  await expect.poll(() => inbound.find((m) => m.includes('"type":"start"'))).toBeTruthy()
  await expect(page.locator('.status-pill.pill-listening')).toBeVisible()

  return { inbound, ws: () => routed! }
}

test('TTS turn drains → client sends playback_done (SSoT)', async ({ page }) => {
  await seedApiKey(page)
  await stubVoiceAudio(page)
  await mockBackend(page, [NOISE])

  const { inbound, ws } = await openVoiceBar(page)

  await ws().send(JSON.stringify({ type: 'tts_start' }))
  await expect(page.locator('.status-pill.pill-speaking')).toBeVisible()

  await ws().send(PCM_CHUNK)                         // one audio chunk
  await ws().send(JSON.stringify({ type: 'tts_end' })) // production ended

  // Once the (fake) chunk's onended fires and the queue is empty, the client
  // reports playback_done — the moment the user stops hearing the bot.
  await expect
    .poll(() => inbound.some((m) => m.includes('"type":"playback_done"')))
    .toBe(true)
})

test('barge-in (tts_interruption) flushes audio and sends NO playback_done', async ({ page }) => {
  await seedApiKey(page)
  await stubVoiceAudio(page)
  await mockBackend(page, [NOISE])

  const { inbound, ws } = await openVoiceBar(page)

  await ws().send(JSON.stringify({ type: 'tts_start' }))
  await ws().send(PCM_CHUNK)
  await expect(page.locator('.status-pill.pill-speaking')).toBeVisible()

  // User barged in: server cancelled and tells the client to flush.
  await ws().send(JSON.stringify({ type: 'tts_interruption', reason: 'user_resumed' }))

  // UI returns to listening (audio flushed), and the interrupted turn must NOT
  // emit playback_done (flush() suppressed it — server already lowered
  // bot_speaking on the cancel).
  await expect(page.locator('.status-pill.pill-listening')).toBeVisible()
  await page.waitForTimeout(200) // give any stray onended a beat to (not) fire
  expect(inbound.some((m) => m.includes('"type":"playback_done"'))).toBe(false)
})
