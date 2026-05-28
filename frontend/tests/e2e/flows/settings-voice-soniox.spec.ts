/**
 * E2E flow: Settings → Voice — Soniox plug-and-play.
 *
 * Guards the user-visible path that lets a fresh user enable the cloud
 * Soniox TTS + STT pair from the Voice tab:
 *
 *   1. Soniox cards appear in BOTH panels (TTS chat + STT) — the UI is
 *      registry-driven (``v-for="(spec, id) in engines.tts/stt"``), so
 *      the only way they'd be missing is a backend regression. The spec
 *      asserts the card label is visible inside the right panel.
 *   2. Selecting Soniox STT reveals an "Secret · api_key" row with a
 *      masked input + "Save key" button (mirrors the TTS-side secret
 *      UI). The pill starts at "not set", and ``reqBadgeFor(sttBackendId)``
 *      shows "set api_key" until the key is saved.
 *   3. Pasting a key + clicking Save fires POST
 *      /api/voice/secrets/soniox/api_key. The fixture flips the next
 *      ``GET /api/voice/secrets`` and ``GET /api/voice/requirements/soniox``
 *      to "key present / ready", so the UI's "stored · hidden" and
 *      "ready" pills must visibly update — proves the panel re-reads
 *      state post-save instead of trusting the prior fetch.
 *   4. Clicking "Save changes" persists the combined active config via
 *      POST /api/voice/active. The recorder asserts the body contains
 *      both ``tts_chat.engine === 'soniox'`` and ``stt.backend === 'soniox'``
 *      — Settings → Voice writes both in a single payload.
 *
 * Why a happy-path-only flow: the unit + backend e2e tests already cover
 * validator failures, WS protocol errors, and the SSoT contract that
 * Soniox shares one ``api_key`` slot across TTS+STT. The Playwright cost
 * is best spent on the live click-flow most users will follow.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('Soniox surfaces in both panels, secret save flips pills, active config persists both', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'settings_voice_soniox.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/settings')

  // Click the Voice tab — General is the default mount, so the Voice
  // panel doesn't render until activation. The tab list lives in
  // SettingsView.vue and uses plain buttons keyed by their visible label.
  await page.getByRole('button', { name: 'Voice', exact: true }).click()

  // ── (1) Soniox card appears in BOTH TTS chat AND STT panels ───────────
  //
  // The two panels both use ``.provider-card`` so we scope by the panel's
  // ``<h2>`` heading to disambiguate. ``filter({ has: ... })`` walks the
  // ancestor chain inside the harness so a missing heading fails loud
  // instead of silently matching the wrong panel.
  const ttsPanel = page
    .locator('.panel-card')
    .filter({ has: page.getByRole('heading', { name: /Chat & Notification Voice/i }) })
  const sttPanel = page
    .locator('.panel-card')
    .filter({ has: page.getByRole('heading', { name: /Speech recognition/i }) })

  await expect(
    ttsPanel.getByRole('button', { name: /Soniox TTS \(real-time\)/i }),
  ).toBeVisible()
  await expect(
    sttPanel.getByRole('button', { name: /Soniox STT \(real-time\)/i }),
  ).toBeVisible()

  // ── (2) Selecting Soniox STT reveals the api_key secret row ───────────
  //
  // Before clicking, the STT panel shows faster-whisper params (no
  // secret input). After clicking Soniox STT, the panel must render the
  // "Secret · api_key" field with the "not set" pill and the "Save key"
  // button. We assert on the label text because the field id is
  // dynamically built from the backend id (``stt-secret-soniox-api_key``)
  // and would silently change if someone refactored that template.
  await sttPanel.getByRole('button', { name: /Soniox STT \(real-time\)/i }).click()

  // Anchor on the visible label "Secret · api_key" — placed by the
  // template right above the masked input. The "not set" pill is part
  // of the same <label>, so we assert both at once.
  const sttSecretLabel = sttPanel.getByText('Secret · api_key', { exact: false })
  await expect(sttSecretLabel).toBeVisible()
  await expect(sttPanel.getByText('not set', { exact: true })).toBeVisible()

  // The requirements probe ran on mount; with no key on file the badge
  // says "set api_key".  The badge text is built by ``reqBadgeFor`` —
  // missing slot key surfaces as "set api_key" (joined with the slot name).
  await expect(sttPanel.getByText('set api_key', { exact: false })).toBeVisible()

  // ── (3) Save the key — POST /api/voice/secrets/soniox/api_key ─────────
  //
  // The input id format is ``stt-secret-<backendId>-<slot>``; we use
  // ``getByPlaceholder`` to stay decoupled. The placeholder text comes
  // from the template ("Paste API key" when no value on file).
  const keyInput = sttPanel.getByPlaceholder('Paste API key')
  await keyInput.fill('sk-soniox-test-key')

  const saveKeyBtn = sttPanel.getByRole('button', { name: 'Save key', exact: true })
  await expect(saveKeyBtn).toBeEnabled()
  const [secretResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname === '/api/voice/secrets/soniox/api_key',
    ),
    saveKeyBtn.click(),
  ])
  expect(secretResp.status()).toBe(200)
  const secretCall = recorder.assertContains(
    'POST',
    '/api/voice/secrets/soniox/api_key',
  )
  expect((secretCall.body as { value: string }).value).toBe('sk-soniox-test-key')

  // After save: pill flips to "stored · hidden" and the requirements
  // badge flips to "ready". Without the re-fetch, neither would update —
  // this guards the post-save reload path in ``setSecret``.
  await expect(sttPanel.getByText('stored · hidden', { exact: true })).toBeVisible()
  await expect(sttPanel.getByText('ready', { exact: true })).toBeVisible()

  // ── (4) Also pick Soniox TTS, then Save changes — verify both land ────
  await ttsPanel.getByRole('button', { name: /Soniox TTS \(real-time\)/i }).click()

  const saveAllBtn = page.getByRole('button', { name: 'Save changes', exact: true })
  await expect(saveAllBtn).toBeEnabled()
  const [activeResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname === '/api/voice/active',
    ),
    saveAllBtn.click(),
  ])
  expect(activeResp.status()).toBe(200)

  const activeCall = recorder.assertContains('POST', '/api/voice/active')
  const body = activeCall.body as {
    tts_chat: { engine: string; params: Record<string, unknown> }
    stt: {
      backend: string
      params: Record<string, unknown>
      wake_word: { backend: string }
    }
  } | null
  expect(body?.tts_chat.engine).toBe('soniox')
  expect(body?.stt.backend).toBe('soniox')
  // Default params from the registry should be carried through verbatim —
  // proves changeChatEngine / changeSttBackend seed defaults rather than
  // leaving params empty (which would make the backend's validator pass
  // but the runtime fail when building the provider).
  expect(body?.tts_chat.params.model).toBe('tts-rt-v1')
  expect(body?.tts_chat.params.voice).toBe('Adrian')
  expect(body?.stt.params.model).toBe('stt-rt-v4')
  expect(body?.stt.wake_word.backend).toBe('off')

  if (backend.unexpected.length > 0) {
    console.log('UNEXPECTED:', JSON.stringify(backend.unexpected, null, 2))
  }
  expect(backend.unexpected.length).toBe(0)
})
