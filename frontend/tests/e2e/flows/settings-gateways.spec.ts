/**
 * E2E flow: Settings → Messaging Gateways.
 *
 * Happy path through production code (only the backend is mocked):
 *   1. Open the panel → Telegram + Zalo cards render from GET /api/gateways.
 *   2. Paste a token + Test Connection → POST /api/gateways/telegram/test,
 *      the valid-token success message appears (no silent pass/fail).
 *   3. Enable + set allow-list + Save → POST /api/settings/bulk carries
 *      telegram_enabled=true and the secret token item; the UI confirms.
 *
 * A second test asserts the panel renders usably at a phone viewport
 * (responsive — cards stack, controls reachable).
 */
import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

function telegramCard(page: import('@playwright/test').Page) {
  return page.locator('section.service-card').filter({
    has: page.getByRole('heading', { name: 'Telegram', exact: true }),
  })
}

test('test a token then enable + save Telegram, asserting the bulk POST contract', async ({ page }) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [NOISE, join(FIXTURES, 'settings_gateways.yaml')])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/settings')
  await page.getByRole('button', { name: 'Messaging Gateways', exact: true }).click()

  const card = telegramCard(page)
  await expect(card).toBeVisible()
  // Both registered platforms render (driven by the status endpoint).
  await expect(page.getByRole('heading', { name: 'Zalo', exact: true })).toBeVisible()

  // 1) Test connection — valid token path.
  await card.getByPlaceholder('Paste your bot token').fill('123456:ABCDEF_test-token')
  const [testResp] = await Promise.all([
    page.waitForResponse(
      (r) => r.request().method() === 'POST' &&
        new URL(r.url()).pathname === '/api/gateways/telegram/test',
    ),
    card.getByRole('button', { name: 'Test Connection', exact: true }).click(),
  ])
  expect(testResp.status()).toBe(200)
  await expect(card.getByText(/jarvis_bot/)).toBeVisible()

  // 2) Enable + allow-list + Save. The checkbox is visually hidden behind a
  // custom switch — click the track the user actually sees.
  await card.locator('.switch-track').click()
  await expect(card.locator('input[type="checkbox"]')).toBeChecked()

  // Security: "*" (allow everyone) must surface a loud warning; specific ids must not.
  const allow = card.getByPlaceholder('123456789, 987654321')
  await allow.fill('*')
  await expect(card.locator('.allow-danger')).toBeVisible()
  await allow.fill('111, 222')
  await expect(card.locator('.allow-danger')).toHaveCount(0)

  const [bulkResp] = await Promise.all([
    page.waitForResponse(
      (r) => r.request().method() === 'POST' &&
        new URL(r.url()).pathname === '/api/settings/bulk',
    ),
    card.getByRole('button', { name: 'Save changes', exact: true }).click(),
  ])
  expect(bulkResp.status()).toBe(200)

  // Assert the bulk body carries the right items (no silent drop of fields).
  const call = recorder.assertContains('POST', '/api/settings/bulk')
  const items = (call.body as { items?: any[] })?.items || []
  expect(items).toContainEqual(expect.objectContaining({
    category: 'gateways', key: 'telegram_enabled', value: 'true',
  }))
  expect(items).toContainEqual(expect.objectContaining({
    category: 'gateways', key: 'telegram_token', value: '123456:ABCDEF_test-token', is_secret: true,
  }))
  expect(items).toContainEqual(expect.objectContaining({
    category: 'gateways', key: 'telegram_allow_from', value: '["111","222"]',
  }))

  // Visible confirmation — not a silent save.
  await expect(card.getByText(/Saved\. Applying/)).toBeVisible()
})

test('responsive: the panel renders and is usable at a phone viewport', async ({ page }) => {
  await page.setViewportSize({ width: 371, height: 659 })
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'settings_gateways.yaml')])

  await page.goto('/settings')
  await page.getByRole('button', { name: 'Messaging Gateways', exact: true }).click()

  const card = telegramCard(page)
  await expect(card).toBeVisible()
  // Controls remain reachable (not clipped/overflowed off-screen).
  await expect(card.getByPlaceholder('Paste your bot token')).toBeVisible()
  await expect(card.getByRole('button', { name: 'Test Connection', exact: true })).toBeVisible()
  // Card must not overflow the viewport width.
  const box = await card.boundingBox()
  expect(box).not.toBeNull()
  expect((box?.width || 0)).toBeLessThanOrEqual(371)
})
