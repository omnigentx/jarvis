/**
 * E2E: backend setup-gate redirect.
 *
 * When `/api/*` returns 503 + `X-Setup-Required: true`, the api.js handler
 * pushes the user to `/setup`. If this flow breaks, a fresh user can't reach
 * the wizard and the app appears dead on first load.
 *
 * Contract (api.js::_handleSetupRequired + App.vue::onSetupRequired):
 *   status === 503 && headers['x-setup-required'] === 'true'
 *     && currentRoute !== '/setup'  →  router.push('/setup')
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')

test('503 setup-required on initial load redirects to /setup', async ({ page }) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, join(FIXTURES, 'setup_gate_redirect.yaml'))

  await page.goto('/')

  // The handler pushes `/setup`; the wizard's child-route default then
  // resolves to `/setup/auth` (SetupRoot redirect). Either is proof that
  // the 503 gate fired — assert on the URL prefix.
  await expect(page).toHaveURL(/\/setup(\/|$)/)

  // Invariant: every /api call the app fired must have a fixture match.
  // If anything lands in `unexpected`, we missed declaring an endpoint and
  // the redirect may have happened for the wrong reason.
  expect(backend.unexpected.length).toBe(0)
})
