import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const NOISE = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures', '_app_boot_noise.yaml')

test('busy voice socket preserves the error and releases a pending microphone', async ({ page }) => {
  await seedApiKey(page)
  await page.addInitScript(() => {
    const probe: { stopped: number; release?: () => void } = { stopped: 0 }
    ;(window as typeof window & { __voiceBusyProbe: typeof probe }).__voiceBusyProbe = probe
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: {
        getUserMedia: () => new Promise((resolve) => {
          probe.release = () => resolve({
            getTracks: () => [{ stop: () => { probe.stopped++ } }],
          })
        }),
      },
    })
  })
  await mockBackend(page, [NOISE])
  await page.routeWebSocket(/\/ws\/voice$/, (socket) => {
    void (async () => {
      await page.waitForFunction(() => Boolean(
        (window as typeof window & { __voiceBusyProbe: { release?: () => void } })
          .__voiceBusyProbe.release,
      ))
      await socket.send(JSON.stringify({
        type: 'error',
        detail: 'Voice session already active; close the existing session first',
      }))
      await socket.close({ code: 1013, reason: 'Voice session already active' })
    })()
  })

  await page.goto('/chat')
  await page.locator('.voice-bar .mic-btn').click()
  const message = page.locator('.voice-bar .err-msg')
  await expect(message).toContainText('already active')

  await page.evaluate(() => (
    window as typeof window & { __voiceBusyProbe: { release: () => void } }
  ).__voiceBusyProbe.release())
  await expect.poll(() => page.evaluate(() => (
    window as typeof window & { __voiceBusyProbe: { stopped: number } }
  ).__voiceBusyProbe.stopped)).toBe(1)
  await expect(message).toContainText('already active')
})
