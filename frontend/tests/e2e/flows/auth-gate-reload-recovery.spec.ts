/**
 * E2E: modal login reloads the page so 401-poisoned views recover.
 *
 * Regression under test (the "401 until manual refresh" bug):
 *   AuthGate is an overlay, NOT a route guard — deep-linking to a view
 *   while logged out mounts it behind the modal. Its onMounted fetch
 *   401s, and (pre-fix) nothing ever replayed it: only SSE composables
 *   and ChatView listened to RESTORED, so every other view stayed
 *   broken after a passkey/API-key login until the user reloaded by
 *   hand.
 *
 * The fix: App.vue listens to RESTORED and reloads the page when the
 * login recovered a LOCKOUT (from === 'unauthenticated'). The reload
 * replays every mount fetch with the fresh session cookie. The boot
 * probe (from === 'unknown') must NOT reload — covered by the negative
 * test below (no reload loop).
 *
 * Flow: deep-link /token-usage logged out → mount fetch 401s → modal →
 * type key → page reloads ITSELF → metrics re-fetched with 200 → data
 * renders. No manual page.reload() anywhere in this spec.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { clearAllAuth, mockBackend, waitForPostLoginReload } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('login after lockout self-reloads and the deep-linked view recovers', async ({
  page,
}) => {
  await clearAllAuth(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'auth_gate_reload_recovery.yaml'),
  ])

  await page.goto('/token-usage')

  // Locked out: gate is up, and the view's mount fetch already 401'd.
  const modal = page.getByRole('dialog', { name: /authentication required/i })
  await expect(modal).toBeVisible()
  await expect
    .poll(() =>
      backend.fulfilled.filter(
        (c) => c.path.startsWith('/api/metrics/tokens') && c.status === 401,
      ).length,
    )
    .toBeGreaterThanOrEqual(1)

  // Login via the modal. The app must reload ITSELF — the spec never
  // calls page.reload().
  await page.locator('#auth-gate-key').fill('test-api-key-e2e')
  const settled = waitForPostLoginReload(page)
  await page.getByRole('button', { name: /continue/i }).click()
  await settled

  // Recovery: modal gone, mount fetch replayed (200 this time), data on
  // screen — previously this required a manual refresh.
  await expect(modal).toBeHidden()
  await expect(page.getByText('1.2M', { exact: true }).first()).toBeVisible()
  const okMetrics = backend.fulfilled.filter(
    (c) => c.path.startsWith('/api/metrics/tokens') && c.status === 200,
  )
  expect(okMetrics.length).toBeGreaterThanOrEqual(1)

  // Negative guard: the post-reload boot probe authenticates with
  // from='unknown' and must NOT trigger another reload. A reload loop
  // would re-run the boot probe over and over — assert the whoami count
  // has gone quiet instead of pinning an exact total (SSE composables may
  // legitimately add a probe, see useSSEConnection's pre-onopen 401 path).
  await page.waitForTimeout(500)
  const countWhoami = () =>
    backend.fulfilled.filter((c) => c.path.startsWith('/api/auth/whoami')).length
  const settledCount = countWhoami()
  await page.waitForTimeout(1000)
  expect(countWhoami()).toBe(settledCount)
  expect(page.url()).toContain('/token-usage')
})
