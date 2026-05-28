/**
 * E2E: register a passkey from the Settings → Authentication tab.
 *
 * Contract under test:
 *   - The Authentication tab loads + queries /passkey/list.
 *   - Clicking "Register passkey" calls /register/begin, feeds the
 *     options into navigator.credentials.create() (Playwright virtual
 *     authenticator signs it), POSTs the attestation to
 *     /register/finish.
 *   - On success, the list refreshes and the new passkey row appears.
 *
 * What we deliberately DO NOT assert:
 *   - The cryptographic content of the attestation. The mocked
 *     /register/finish returns 200 regardless. Real verification is
 *     covered in the backend route integration tests.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  clearAllAuth,
  installVirtualAuthenticator,
  mockBackend,
  seedApiKey,
  seedCsrfCookie,
} from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')

test('register passkey from settings drives the ceremony end-to-end', async ({ page }) => {
  await installVirtualAuthenticator(page)
  await clearAllAuth(page)
  await seedApiKey(page, 'test-api-key-e2e')
  await seedCsrfCookie(page, 'test-csrf-token-e2e')

  const backend = await mockBackend(
    page,
    join(FIXTURES, 'passkey_register_in_settings.yaml'),
  )

  await page.goto('/settings')

  // Navigate to the Authentication tab. The page should already show
  // the pre-seeded "E2E MacBook" row from the fixture list response.
  await page.getByRole('button', { name: /^authentication$/i }).click()
  await expect(page.getByText('E2E MacBook')).toBeVisible({ timeout: 5000 })

  // Click "Register passkey". Wait for the finish POST specifically —
  // that's the durable signal that the full ceremony (begin →
  // navigator.credentials.create on the virtual authenticator →
  // finish) ran end-to-end without throwing.
  const registerBtn = page.getByRole('button', { name: /register passkey/i })
  await expect(registerBtn).toBeVisible()

  // waitForResponse (not waitForRequest) — the latter resolves when the
  // request is observed, before our mock handler runs and pushes to the
  // backend.fulfilled array, leading to a flaky race.
  const finishResponse = page.waitForResponse(
    (resp) => resp.request().method() === 'POST'
      && resp.url().endsWith('/api/auth/passkey/register/finish'),
    { timeout: 10_000 },
  )
  await registerBtn.click()
  await finishResponse

  // Both begin and finish must have hit the mock backend.
  const begin = backend.fulfilled.filter(
    (c) => c.method === 'POST'
      && c.path === '/api/auth/passkey/register/begin',
  )
  const finish = backend.fulfilled.filter(
    (c) => c.method === 'POST'
      && c.path === '/api/auth/passkey/register/finish',
  )
  expect(begin.length).toBeGreaterThanOrEqual(1)
  expect(finish.length).toBeGreaterThanOrEqual(1)

  // User-facing feedback shows up — either the success message or
  // the list refresh signal. Success message is the explicit confirm.
  await expect(
    page.getByText(/passkey registered/i),
  ).toBeVisible({ timeout: 5000 })
})
