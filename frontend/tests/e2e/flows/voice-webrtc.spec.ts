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

import type { Page } from '@playwright/test'
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
  await page.routeWebSocket(/\/ws\/voice$/, (ws) => {
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
})
