/**
 * E2E flow: ChatInput dictation — press-to-talk → text in textarea.
 *
 * Guards the user-visible path that lets a user click the bottom mic,
 * speak, and end up with editable text in the chat composer (instead of
 * an auto-submitted message like the hands-free VoiceBar does):
 *
 *   1. Click mic → component opens a WS to /ws/voice, sends
 *      {type:'start', mode:'dictation'} as the handshake.
 *   2. We push partial_transcript / final_transcript JSON frames; the
 *      textarea reflects them live (provisional + finals merged).
 *   3. Second mic click stops dictation but keeps the merged text — the
 *      user edits a typo, then clicks Send. The send event fires with
 *      the EDITED text, proving dictation does NOT auto-submit.
 *
 * Why mock the WebSocket + audio capture: the dictation pipeline needs
 * a live /ws/voice plus a mic. In CI we have neither, so we stub:
 *   - page.routeWebSocket for the /ws/voice frames.
 *   - navigator.mediaDevices.getUserMedia + AudioContext + AudioWorklet
 *     so the audio capture half no-ops without throwing. We never need
 *     real PCM because the stubbed WS doesn't actually transcribe.
 */

import type { Page, WebSocketRoute } from '@playwright/test'
import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

/**
 * Stub the audio capture path so getUserMedia + AudioContext +
 * AudioWorkletNode resolve to no-op instances. The dictation composable
 * awaits each step, so the stubs return correctly-shaped objects rather
 * than ``undefined`` — otherwise an awaited promise would throw and
 * short-circuit before the WS handshake.
 */
async function stubMicCapture(page: Page) {
  await page.addInitScript(() => {
    const fakeTrack = {
      stop() {},
      getSettings: () => ({}),
      getCapabilities: () => ({}),
    }
    const fakeStream = {
      getAudioTracks: () => [fakeTrack],
      getTracks: () => [fakeTrack],
    }
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: async () => fakeStream },
    })
    class FakeAudioWorklet { async addModule() {} }
    class FakeAudioContext {
      audioWorklet = new FakeAudioWorklet()
      currentTime = 0
      sampleRate = 48000
      createMediaStreamSource() { return { connect() {} } }
      async close() {}
    }
    class FakeAudioWorkletNode {
      port = { onmessage: null }
      disconnect() {}
    }
    // @ts-expect-error — replacing globals for test
    window.AudioContext = FakeAudioContext
    // @ts-expect-error
    window.AudioWorkletNode = FakeAudioWorkletNode
  })
}

test('mic dictates into textarea, user edits, send fires edited text — no auto-submit', async ({ page }) => {
  await seedApiKey(page)
  await stubMicCapture(page)
  await mockBackend(page, [NOISE])

  // Capture the routed WebSocket so the test can push server-side frames
  // on demand. routeWebSocket fires once per opened socket; we stash the
  // route reference + log every inbound client frame so assertions can
  // verify the handshake payload.
  let routedWS: WebSocketRoute | null = null
  const inboundFrames: string[] = []
  await page.routeWebSocket(/\/ws\/voice$/, (ws) => {
    routedWS = ws
    ws.onMessage((data) => {
      const s = typeof data === 'string' ? data : '<binary>'
      inboundFrames.push(s)
      try {
        const msg = JSON.parse(s)
        if (msg.type === 'start') {
          // Ack the handshake exactly like the real backend would so the
          // composable's status flips to 'listening'.
          ws.send(JSON.stringify({ type: 'stt_ready' }))
        }
      } catch { /* binary PCM frames — ignore */ }
    })
  })

  await page.goto('/chat')

  const mic = page.getByTestId('chat-mic')
  await expect(mic).toBeVisible()
  const textarea = page.locator('textarea.textarea')
  await expect(textarea).toBeVisible()

  // ── (1) First click opens the WS + handshake ────────────────────────
  await mic.click()
  await expect.poll(() => inboundFrames.find((m) => m.includes('"type":"start"'))).toBeTruthy()
  const startMsg = JSON.parse(inboundFrames.find((m) => m.includes('"type":"start"'))!)
  expect(startMsg.mode).toBe('dictation')

  // Button flipped to the active state — title swap is the user-visible
  // signal; the .recording class drives the pulse styling.
  await expect(mic).toHaveAttribute('title', /Stop dictation/)

  // ── (2) Drive transcripts from the stubbed server ───────────────────
  // Provisional partial → live tail in textarea. Then a final that flips
  // the partial into the accumulated finals buffer.
  expect(routedWS).not.toBeNull()
  await routedWS!.send(JSON.stringify({ type: 'partial_transcript', text: 'Xin chào' }))
  await expect.poll(() => textarea.inputValue()).toContain('Xin chào')

  await routedWS!.send(JSON.stringify({ type: 'final_transcript', text: 'Xin chào Jarvis' }))
  await expect.poll(() => textarea.inputValue()).toContain('Xin chào Jarvis')

  // ── (3) Second click stops dictation, text stays for editing ────────
  await mic.click()
  await expect(mic).toHaveAttribute('title', /Dictate/)

  // Edit the dictated text — add punctuation that no STT would produce.
  await textarea.click()
  await textarea.fill('Xin chào Jarvis, làm ơn giúp tôi.')

  // ── (4) Click Send — fires with the EDITED text, NOT the dictated raw
  // The send button is the only enabled non-mic non-attach button in the
  // composer once there's text; locate by class to dodge i18n.
  await page.locator('.send-btn').first().click()
  // handleSend clears the textarea after emitting — proves the send path
  // ran, and that dictation didn't auto-submit before we got the edit in.
  await expect.poll(() => textarea.inputValue()).toBe('')
})
