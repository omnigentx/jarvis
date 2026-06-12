/**
 * E2E: VoiceBar negotiates WebRTC audio.
 *
 * Verifies the frontend half of the iOS-AEC fix: when the mic is toggled, the
 * client creates an RTCPeerConnection and sends a valid ``webrtc_offer`` over
 * /ws/voice (a real audio SDP), instead of streaming PCM over the WS. This runs
 * with Chromium's fake media device so getUserMedia yields a REAL
 * MediaStreamTrack (the stubbed object used by other specs can't be addTrack'd,
 * so they exercise the WS fallback instead).
 *
 * What this does NOT cover (needs a real device): the media actually flowing and
 * the browser AEC scrubbing the echo — that's the manual test. Here we prove the
 * negotiation initiates correctly from production code.
 */

import type { Page, WebSocketRoute } from '@playwright/test'
import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

/**
 * Hand getUserMedia a REAL (silent) MediaStreamTrack via an AudioContext
 * MediaStreamDestination. Unlike a plain stub object, a real track can be
 * ``RTCPeerConnection.addTrack``'d — which is what drives the WebRTC offer.
 * More reliable than --use-fake-device flags (which didn't take headless here).
 */
async function stubRealMicStream(page: Page) {
  await page.addInitScript(() => {
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: {
        getUserMedia: async () => {
          const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
          const ac = new AC()
          const dest = ac.createMediaStreamDestination()
          return dest.stream
        },
      },
    })
  })
}

test('mic toggle → client sends a valid webrtc_offer (audio over WebRTC)', async ({ page }) => {
  await seedApiKey(page)
  await stubRealMicStream(page)
  await mockBackend(page, [NOISE])

  const inbound: string[] = []
  let routed: WebSocketRoute | null = null
  await page.routeWebSocket(/\/ws\/voice$/, (ws) => {
    routed = ws
    ws.onMessage((data) => {
      const s = typeof data === 'string' ? data : '<binary>'
      inbound.push(s)
      try {
        if (JSON.parse(s).type === 'start') ws.send(JSON.stringify({ type: 'stt_ready' }))
      } catch { /* binary */ }
    })
  })

  await page.goto('/chat')
  const mic = page.locator('.voice-bar .mic-btn')
  await expect(mic).toBeVisible()
  await mic.click()

  // The negotiation must produce a webrtc_offer with a real audio m-line.
  await expect
    .poll(() => inbound.find((m) => m.includes('"type":"webrtc_offer"')), { timeout: 8000 })
    .toBeTruthy()
  const offer = JSON.parse(inbound.find((m) => m.includes('"type":"webrtc_offer"'))!)
  expect(offer.sdp_type).toBe('offer')
  expect(offer.sdp).toContain('m=audio')
  expect(offer.sdp).toContain('v=0')

  // And it must NOT stream mic PCM over the WS in WebRTC mode (audio rides the
  // peer connection). Allow control frames; reject binary.
  expect(inbound.some((m) => m === '<binary>')).toBe(false)

  // Regression guard for the WebRTC bot_speaking SSoT: in WebRTC mode the SERVER
  // owns the drain edge (its real-time track pacing), so the client must NOT
  // send playback_done. If it did, the server would clear bot_speaking at
  // tts_end — while the track is still playing — and kill onset barge-in during
  // the tail. Drive a full TTS turn and assert no playback_done is ever sent.
  expect(routed).not.toBeNull()
  await routed!.send(JSON.stringify({ type: 'tts_start' }))
  await routed!.send(JSON.stringify({ type: 'tts_end' }))
  await page.waitForTimeout(300)
  expect(inbound.some((m) => m.includes('"type":"playback_done"'))).toBe(false)
})

/**
 * Track every RTCPeerConnection the app creates and let the test force the
 * NEWEST one into a connection state — the only way to exercise the ICE-death
 * recovery path headless (Playwright can't make a real ICE pair die on cue).
 * The composable's own ``connectionstatechange`` listener receives the event,
 * so everything downstream (webrtcRecovery policy, re-offer, give-up banner)
 * is production code.
 */
async function trackPeerConnections(page: Page) {
  await page.addInitScript(() => {
    const pcs: RTCPeerConnection[] = []
    const Orig = window.RTCPeerConnection
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(window as any).RTCPeerConnection = class extends Orig {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      constructor(...args: any[]) {
        super(...args)
        pcs.push(this)
      }
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(window as any).__pcCount = () => pcs.length
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(window as any).__failNewestPc = () => {
      const pc = pcs[pcs.length - 1]
      Object.defineProperty(pc, 'connectionState', { configurable: true, get: () => 'failed' })
      pc.dispatchEvent(new Event('connectionstatechange'))
    }
  })
}

test('ICE failure mid-session → re-offers over the live WS; exhausted retries → error banner', async ({ page }) => {
  await seedApiKey(page)
  await stubRealMicStream(page)
  await trackPeerConnections(page)
  await mockBackend(page, [NOISE])

  const offers: string[] = []
  await page.routeWebSocket(/\/ws\/voice$/, (ws) => {
    ws.onMessage((data) => {
      const s = typeof data === 'string' ? data : '<binary>'
      if (s.includes('"type":"webrtc_offer"')) offers.push(s)
      try {
        if (JSON.parse(s).type === 'start') ws.send(JSON.stringify({ type: 'stt_ready' }))
      } catch { /* binary */ }
    })
  })

  await page.goto('/chat')
  const mic = page.locator('.voice-bar .mic-btn')
  await expect(mic).toBeVisible()
  await mic.click()
  await expect.poll(() => offers.length, { timeout: 8000 }).toBe(1)

  // First ICE death (routine on 5G: handover/NAT rebind) → the client must
  // re-negotiate with a fresh PC over the still-open WS, NOT show the fatal
  // banner (the prod bug this pins).
  await page.evaluate(() => (window as unknown as { __failNewestPc: () => void }).__failNewestPc())
  await expect.poll(() => offers.length, { timeout: 8000 }).toBe(2)
  await expect(page.locator('.voice-bar .err-msg')).toHaveCount(0)

  // Second consecutive death → second (last-budget) reconnect attempt.
  await page.evaluate(() => (window as unknown as { __failNewestPc: () => void }).__failNewestPc())
  await expect.poll(() => offers.length, { timeout: 8000 }).toBe(3)

  // Third consecutive death → budget exhausted → fail loud, no more offers.
  await page.evaluate(() => (window as unknown as { __failNewestPc: () => void }).__failNewestPc())
  await expect(page.locator('.voice-bar .err-msg')).toContainText('WebRTC could not connect')
  await page.waitForTimeout(500)
  expect(offers.length).toBe(3)
})
