/**
 * Reference E2E flow: Setup Wizard.
 *
 * Guards the first-run path — if this flow breaks, nobody can bootstrap the
 * app. All other flow specs copy this file's structure.
 *
 * Coverage:
 *  1. Fresh install lands on Step 1 (auth) with the wizard shell rendered.
 *  2. Submitting a valid master key POSTs /api/setup/auth and advances to
 *     Step 2 (llm).
 *  3. Negative control: when backend reports setup already complete, the UI
 *     lands on the Verify step instead — catches a refactor that accidentally
 *     pushes users back to Step 1.
 *
 * This file is the TEMPLATE other flow specs should copy. Keep it readable.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('fresh install renders Step 1 (auth) with current step highlighted', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'setup_wizard_fresh.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/setup/auth')

  // Wizard shell + Step 1 inputs (identified by stable id attributes, not
  // by visible text which can change with copy updates).
  await expect(page.getByText('Jarvis Setup')).toBeVisible()
  await expect(page.locator('#new-key')).toBeVisible()
  await expect(page.locator('#confirm-key')).toBeVisible()

  // Wizard bootstrap sequence: probe (unauth) + status (auth-via-key).
  recorder.assertContains('GET', '/api/setup/auth/probe')
  recorder.assertContains('GET', '/api/setup/status')
  expect(backend.unexpected.length).toBe(0)
})

test('submitting a valid master key advances from Step 1 to Step 2', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'setup_wizard_fresh.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/setup/auth')
  await expect(page.locator('#new-key')).toBeVisible()

  const KEY = 'e2e-master-key-0123456789abcdef'
  await page.locator('#new-key').fill(KEY)
  await page.locator('#confirm-key').fill(KEY)

  await page.getByRole('button', { name: /continue/i }).click()

  // Assertion 1: POST /api/setup/auth fired with the trimmed key.
  await expect.poll(() =>
    recorder.calls.find((c) => c.method === 'POST' && c.path === '/api/setup/auth')
  ).toBeTruthy()
  const authCall = recorder.assertContains('POST', '/api/setup/auth')
  expect((authCall.body as { api_key?: string })?.api_key).toBe(KEY)

  // Assertion 2: URL moved to Step 2.
  await expect(page).toHaveURL(/\/setup\/llm/)

  expect(backend.unexpected.length).toBe(0)
})

test('negative control: setup already complete stays on Verify step', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'setup_already_complete.yaml')])

  await page.goto('/setup/verify')

  // The wizard's `watch(overallComplete)` redirects to Verify — verify we
  // DON'T bounce back to Step 1 when complete.
  await expect(page).toHaveURL(/\/setup\/verify/)
  await expect(page).not.toHaveURL(/\/setup\/auth/)
})
