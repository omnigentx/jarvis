/**
 * E2E: Settings → Context Compaction.
 *
 * Contract under test:
 *   - The tab loads current config from GET /api/context-compaction/settings
 *     and renders typed inputs (threshold ratio, keep-recent, versions
 *     visible, enable toggles).
 *   - Save is disabled until something changes; the PATCH body carries
 *     ONLY the changed keys (audit history reflects real edits).
 *   - Success feedback appears after save; a 422 from the backend surfaces
 *     the validation message inline instead of failing silently.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

async function openCompactionTab(page) {
  await page.goto('/settings')
  await page.getByRole('button', { name: 'Context Compaction' }).click()
}

test('loads config, save sends only changed keys, success shows', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'settings_compaction.yaml')])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await openCompactionTab(page)

  // Form seeded from GET.
  const ratioRow = page.locator('.row', { hasText: 'Compaction threshold' })
  await expect(ratioRow.locator('input')).toHaveValue('0.7')
  const versionsRow = page.locator('.row', { hasText: 'Versions shown' })
  await expect(versionsRow.locator('input')).toHaveValue('3')

  // Save disabled until dirty.
  const saveBtn = page.getByRole('button', { name: /Save Changes/ })
  await expect(saveBtn).toBeDisabled()

  await ratioRow.locator('input').fill('0.8')
  await versionsRow.locator('input').fill('5')
  await expect(page.getByText('2 unsaved change(s)')).toBeVisible()
  await expect(saveBtn).toBeEnabled()
  await saveBtn.click()

  await expect(page.getByText(/Saved · live now/)).toBeVisible()

  // PATCH carried ONLY the two changed keys.
  const patch = recorder.calls.find(
    (c) => c.method === 'PATCH' && c.path === '/api/context-compaction/settings',
  )
  expect(patch, 'PATCH must have fired').toBeTruthy()
  expect(patch!.body).toEqual({ compact_at_ratio: 0.8, snapshot_versions_visible: 5 })

  // Form re-seeded from the response → no more unsaved changes.
  await expect(saveBtn).toBeDisabled()
})

test('backend 422 surfaces the validation message inline', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'settings_compaction.yaml')])
  await page.route('**/api/context-compaction/settings', (route) => {
    if (route.request().method() !== 'PATCH') return route.fallback()
    return route.fulfill({
      status: 422,
      contentType: 'application/json',
      body: JSON.stringify({
        detail: 'compact_at_ratio must be between 0.3 and 0.95',
      }),
    })
  })

  await openCompactionTab(page)

  const ratioRow = page.locator('.row', { hasText: 'Compaction threshold' })
  await ratioRow.locator('input').fill('0.2')
  await page.getByRole('button', { name: /Save Changes/ }).click()

  await expect(
    page.getByText('compact_at_ratio must be between 0.3 and 0.95'),
  ).toBeVisible()
})
