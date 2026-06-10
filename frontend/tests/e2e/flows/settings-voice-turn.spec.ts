/**
 * E2E flows: Cloudflare TURN credential entry — Settings → Voice AND the
 * Setup Wizard services step.
 *
 * Why this exists: voice over WebRTC silently has NO path from networks
 * outside the host's LAN/VPN unless a TURN relay is configured (the
 * 2026-06-10 "iPhone on 5G — ICE failed" incident). The fix ships as a
 * registry-driven VOICE_SERVICES entry; these flows guard the two surfaces
 * a user can configure it from:
 *
 *   1. Settings → Voice: the TURN panel renders from GET /api/voice/engines
 *      ``services`` (a backend regression hiding the key would kill the
 *      panel), the setup walkthrough is visible, and saving each slot fires
 *      POST /api/voice/secrets/cloudflare_turn/{slot} with the pasted value.
 *      Pills must flip on re-fetch ("not set" → "stored · hidden", badge
 *      "set key_id · api_token" → "configured — relay active").
 *   2. Setup Wizard step 3: the TURN card renders, partial fill blocks
 *      submit (requireAllIfAny), and the final POST /api/setup/services
 *      carries {cloudflare_turn: {key_id, api_token}} — the backend maps it
 *      into the SAME voice.secrets.* slots (covered by backend tests).
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('Settings → Voice: TURN panel renders, both slots save, pills flip', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'settings_voice_turn.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/settings')
  await page.getByRole('button', { name: 'Voice', exact: true }).click()

  // ── (1) Registry-driven panel + walkthrough ────────────────────────────
  const panel = page.getByTestId('voice-service-cloudflare_turn')
  await expect(panel).toBeVisible()
  await expect(
    panel.getByRole('heading', { name: /Cloudflare TURN \(voice relay\)/i }),
  ).toBeVisible()
  // The "when do I need this" guidance is the OSS-user contract — a fresh
  // self-hoster must be able to tell whether this section applies to them.
  await expect(panel.getByText('When do I need this?', { exact: false })).toBeVisible()
  await expect(panel.getByText('localhost', { exact: false }).first()).toBeVisible()
  // Mount probe: neither slot set → badge lists both (reqBadgeFor joins
  // missing slots with ", ").
  await expect(panel.getByText('set key_id, api_token', { exact: false })).toBeVisible()

  // ── (2) Save key_id ────────────────────────────────────────────────────
  await panel.locator('#svc-secret-cloudflare_turn-key_id').fill('cf-key-id-123')
  const keyIdRow = panel.locator('.field', {
    has: page.locator('#svc-secret-cloudflare_turn-key_id'),
  })
  const [keyIdResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname === '/api/voice/secrets/cloudflare_turn/key_id',
    ),
    keyIdRow.getByRole('button', { name: 'Save key', exact: true }).click(),
  ])
  expect(keyIdResp.status()).toBe(200)
  const keyIdCall = recorder.assertContains(
    'POST',
    '/api/voice/secrets/cloudflare_turn/key_id',
  )
  expect((keyIdCall.body as { value: string }).value).toBe('cf-key-id-123')

  // ── (3) Save api_token — badge flips to the configured state ──────────
  await panel.locator('#svc-secret-cloudflare_turn-api_token').fill('cf-api-token-456')
  const tokenRow = panel.locator('.field', {
    has: page.locator('#svc-secret-cloudflare_turn-api_token'),
  })
  const [tokenResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname === '/api/voice/secrets/cloudflare_turn/api_token',
    ),
    tokenRow.getByRole('button', { name: 'Save key', exact: true }).click(),
  ])
  expect(tokenResp.status()).toBe(200)

  await expect(panel.getByText('configured — relay active', { exact: true })).toBeVisible()
  // Both slots now report stored.
  await expect(panel.getByText('stored · hidden', { exact: true })).toHaveCount(2)

  if (backend.unexpected.length > 0) {
    console.log('UNEXPECTED:', JSON.stringify(backend.unexpected, null, 2))
  }
  expect(backend.unexpected.length).toBe(0)
})

test('Setup Wizard: TURN card collects both values into POST /api/setup/services', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'wizard_services_turn.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/setup/services')

  const card = page.getByTestId('wizard-service-cloudflare_turn')
  await expect(card).toBeVisible()
  // The card copy must tell a fresh user when they need this and that it
  // is skippable — the OSS onboarding contract.
  await expect(card.getByText('OUTSIDE your home network', { exact: false })).toBeVisible()
  await expect(card.getByText('Not needed for localhost', { exact: false })).toBeVisible()

  await card.getByRole('button', { name: 'Configure', exact: true }).click()

  // requireAllIfAny: filling only one field blocks submit with a visible error.
  await card.locator('#cloudflare_turn-key_id').fill('cf-key-id-123')
  await expect(
    page.getByText(/Cloudflare TURN.*please also fill/i),
  ).toBeVisible()

  await card.locator('#cloudflare_turn-api_token').fill('cf-api-token-456')
  await expect(page.getByText(/please also fill/i)).toHaveCount(0)

  const [resp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname === '/api/setup/services',
    ),
    page.getByRole('button', { name: /Save & Continue/i }).click(),
  ])
  expect(resp.status()).toBe(200)

  const call = recorder.assertContains('POST', '/api/setup/services')
  const body = call.body as {
    services: Record<string, Record<string, string>>
  }
  expect(body.services.cloudflare_turn).toEqual({
    key_id: 'cf-key-id-123',
    api_token: 'cf-api-token-456',
  })

  if (backend.unexpected.length > 0) {
    console.log('UNEXPECTED:', JSON.stringify(backend.unexpected, null, 2))
  }
  expect(backend.unexpected.length).toBe(0)
})
