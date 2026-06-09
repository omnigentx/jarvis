/**
 * E2E flow: mobile FAB quick hide/show via the grip (375px).
 *
 * The floating chat + voice FABs cover content on small screens. The grip lets
 * the user hide them in one tap (works even with no scroll) and is the only way
 * back once hidden — auto-scroll must not override a manual hide. Persisted.
 */
import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test.use({ viewport: { width: 375, height: 659 } })

test('grip hides + shows the FABs, and remembers the hidden choice', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'approvals_cron_pending.yaml')])
  // Start from a clean preference so the test is order-independent.
  await page.addInitScript(() => {
    try { localStorage.removeItem('jarvis.fabsManualHidden') } catch (_) {}
  })

  await page.goto('/approvals')

  const grip = page.locator('.fab-grip')
  const dock = page.locator('.dock-fab')
  await expect(grip).toBeVisible()
  await expect(dock).toBeVisible()
  await expect(dock).not.toHaveClass(/dock-fab--hidden/)

  // Tap grip → FABs hide (slid off-edge, non-interactive).
  await grip.click()
  await expect(dock).toHaveClass(/dock-fab--hidden/)
  expect(await page.evaluate(() => localStorage.getItem('jarvis.fabsManualHidden'))).toBe('1')
  // Grip itself stays reachable so the user can bring them back.
  await expect(grip).toBeVisible()

  // Tap grip again → FABs show.
  await grip.click()
  await expect(dock).not.toHaveClass(/dock-fab--hidden/)
  expect(await page.evaluate(() => localStorage.getItem('jarvis.fabsManualHidden'))).toBe('0')
})

test('manual hide persists across navigation (sticky preference)', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'approvals_cron_pending.yaml')])
  await page.addInitScript(() => {
    try { localStorage.setItem('jarvis.fabsManualHidden', '1') } catch (_) {}
  })

  await page.goto('/approvals')
  // Restored from storage → FABs start hidden without any interaction.
  await expect(page.locator('.dock-fab')).toHaveClass(/dock-fab--hidden/)
})
