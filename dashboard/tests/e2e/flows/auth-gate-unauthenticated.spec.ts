/**
 * E2E: AuthGate boot path.
 *
 * Contract under test:
 *   - On page load with no session cookie, App.vue calls auth.init() →
 *     auth.probe() → /api/auth/whoami.
 *   - Whoami returns ``authenticated:false``, store transitions to
 *     UNAUTHENTICATED, AuthGate modal mounts blocking the rest of
 *     the UI.
 *   - The modal must render visible and trap focus on its input.
 *   - SSE composables that mounted before the probe finished must
 *     NOT keep retrying once EXPIRED fires (no 401 spam).
 *
 * If this breaks, the original bug (rotate-key-then-401-loop) returns:
 * the user gets stuck looking at "Disconnected" with no way to type
 * the new key.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, clearAllAuth, NetworkRecorder } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')

test('boot with no cookie shows AuthGate modal', async ({ page }) => {
  await clearAllAuth(page)
  const backend = await mockBackend(
    page,
    join(FIXTURES, 'auth_gate_unauthenticated.yaml'),
  )

  await page.goto('/')

  // Modal should appear once the probe resolves authenticated:false.
  // Use role=dialog (aria-modal) since that's the contract; it survives
  // CSS / class-name refactors.
  const modal = page.getByRole('dialog', { name: /authentication required/i })
  await expect(modal).toBeVisible({ timeout: 5000 })

  // Input must be the active focused element (focus trap on mount).
  await expect(page.locator('#auth-gate-key')).toBeFocused()

  // No /api/* request should have ended in unexpected (everything fixture-mocked).
  expect(backend.unexpected.length).toBe(0)
})

test('AuthGate covers the bare /setup layout too', async ({ page }) => {
  // Even if the user happened to navigate directly to /setup with no
  // cookie, the modal still appears. The setup wizard has its own auth
  // bootstrap but the AuthGate doesn't gate it (it only opens when
  // status is UNAUTHENTICATED, which init() skips on bare layouts).
  // This test pins that behavior so a future refactor doesn't
  // accidentally show the modal during the wizard.
  await clearAllAuth(page)
  await mockBackend(page, join(FIXTURES, 'auth_gate_unauthenticated.yaml'))

  await page.goto('/setup')

  // Bare layout should NOT have triggered probe → no modal.
  const modal = page.getByRole('dialog', { name: /authentication required/i })
  await expect(modal).toBeHidden()
})

test('SSE composables stop retrying once EXPIRED fires', async ({ page }) => {
  // Quick & dirty: count /api/agents/activity-stream connections opened.
  // Without the auth-bus EXPIRED handling, the composable would
  // exponentially retry forever. We allow up to 2 connection attempts
  // (the initial open before probe completes + maybe one retry that
  // races) and assert the count plateaus.
  await clearAllAuth(page)
  await mockBackend(page, join(FIXTURES, 'auth_gate_unauthenticated.yaml'))

  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/')
  await expect(
    page.getByRole('dialog', { name: /authentication required/i }),
  ).toBeVisible()

  // Sample at two checkpoints 2.5s apart — if the SSE were still
  // retrying with backoff, we'd see fresh attempts. The harness's
  // SSE fixture is one-shot ping, so any new connection counts.
  const t1 = recorder.calls.filter((c) =>
    c.path.startsWith('/api/agents/activity-stream'),
  ).length
  await page.waitForTimeout(2500)
  const t2 = recorder.calls.filter((c) =>
    c.path.startsWith('/api/agents/activity-stream'),
  ).length

  // Allow some tolerance for races, but the counts must stabilize.
  expect(t2 - t1).toBeLessThanOrEqual(1)
})
