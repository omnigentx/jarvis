/**
 * E2E flow: TTS preview speaks the language of the SELECTED voice.
 *
 * A Vietnamese sample read by an English voice (or vice versa) sounds
 * broken and tells the user nothing about real voice quality. The
 * preview body must therefore follow the voice's locale, not a fixed
 * bilingual string:
 *
 *   - Edge voice `vi-VN-NamMinhNeural` (locale prefix) → Vietnamese sample
 *   - Soniox with `language: 'en'` (explicit param)    → English sample
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('preview text follows the selected voice language (vi voice → vi, en voice → en)', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'settings_voice_soniox.yaml'),
    join(FIXTURES, 'settings_voice_preview_overlay.yaml'),
  ])

  await page.goto('/settings')
  await page.getByRole('button', { name: 'Voice', exact: true }).click()

  const ttsPanel = page
    .locator('.panel-card')
    .filter({ has: page.getByRole('heading', { name: /Chat & Notification Voice/i }) })

  // ── Edge (default), voice vi-VN-NamMinhNeural → Vietnamese sample ──────
  const [viResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname === '/api/voice/test/tts',
    ),
    ttsPanel.getByRole('button', { name: 'Preview', exact: true }).click(),
  ])
  const viBody = JSON.parse(viResp.request().postData() || '{}')
  expect(viBody.text).toBe('Xin chào, đây là bản xem trước giọng đọc.')
  expect(viBody.engine).toBe('edge')

  // ── Switch to Soniox TTS (language param defaults to 'en') → English ───
  await ttsPanel.getByRole('button', { name: /Soniox TTS \(real-time\)/i }).click()
  const [enResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname === '/api/voice/test/tts',
    ),
    ttsPanel.getByRole('button', { name: 'Preview', exact: true }).click(),
  ])
  const enBody = JSON.parse(enResp.request().postData() || '{}')
  expect(enBody.text).toBe('Hello, this is a voice preview.')
  expect(enBody.engine).toBe('soniox')
})
