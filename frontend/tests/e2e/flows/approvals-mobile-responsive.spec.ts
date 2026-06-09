/**
 * E2E flow: Approvals detail header is responsive on mobile (375px).
 *
 * Regression guard for the mobile bug where the detail header
 * (badge · title · Preview/Source toggle) stayed a flex ROW on phones, so the
 * title's flex item shrank to min-width:0 instead of wrapping — crushing a long
 * Vietnamese title into a ~3-char column, one word per line.
 *
 * The fix stacks the header vertically on mobile; here we assert the rendered
 * title spans most of the card width (it would be a narrow sliver if crushed).
 */
import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

// Phone viewport (matches the 375×659 class used in the QA report).
test.use({ viewport: { width: 375, height: 659 } })

test('mobile: long approval title gets full width, not crushed into a column', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'approvals_cron_pending.yaml')])

  await page.goto('/approvals')

  // Open the (only) pending approval → mobile shows the detail pane.
  await page.locator('.approvals-row').first().click()

  const title = page.locator('.approvals__detail-title')
  await expect(title).toBeVisible()
  await expect(title).toHaveText(/Kiểm tra thời tiết Gia Lâm buổi sáng/)

  const box = await title.boundingBox()
  expect(box).not.toBeNull()
  // On a 375px screen the stacked title should span most of the card. When the
  // bug is present the title is squeezed beside the badge + toggle to a sliver
  // (~30–60px) and wraps one word per line (very tall). Assert it's wide…
  expect(box!.width).toBeGreaterThan(240)
  // …and therefore NOT a tall vertical strip (a crushed title was 10+ lines).
  expect(box!.height).toBeLessThan(120)
})
