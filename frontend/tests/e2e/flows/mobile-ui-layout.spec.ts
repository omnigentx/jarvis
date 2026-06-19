/**
 * E2E: mobile layout regressions (375px phone viewport).
 *
 * Three bugs reported from on-device QA, each guarded here:
 *
 *  1. CHAT — the composer floated ~80px above the bottom tab bar (a band of
 *     dead space). Cause: ChatView bled only part of AppLayout's content
 *     padding-bottom, leaving the --mobile-fab-band (64px) as a visible gap.
 *     Guard: composer sits within ~32px of the tab bar.
 *
 *  2. MONITOR — the "INJECT TO N agents" bar was position:sticky and honoured
 *     the content padding-bottom, so it pinned mid-screen, floating over the
 *     agent stream. Fix: inline (static) on mobile. Guard: not sticky.
 *
 *  3. APPROVALS — a wide code block in the preview pushed the whole page into
 *     horizontal scroll on iOS Safari (grid item didn't shrink below its
 *     content min-content). Fix: min-width:0 on the grid items. Guard: the
 *     document never exceeds the viewport width.
 */
import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test.use({ viewport: { width: 375, height: 659 } })

test('mobile chat: composer sits just above the tab bar (no dead-space gap)', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'chat_streaming_happy.yaml')])
  await page.goto('/chat')

  // Wait for the SPA to render both anchors before measuring — querying them
  // synchronously right after goto() races the first paint (a null here is a
  // not-yet-mounted composer, not a layout bug).
  await expect(page.locator('.compose-host')).toBeVisible()
  await expect(page.locator('.mobile-tabbar')).toBeVisible()

  const gap = await page.evaluate(() => {
    const compose = document.querySelector('.compose-host')!.getBoundingClientRect()
    const tabbar = document.querySelector('.mobile-tabbar')!.getBoundingClientRect()
    return tabbar.top - compose.bottom
  })
  // The intended breathing room is ~16px (max(16px, safe-area)). The bug left
  // ~80px. Anything under 32px proves the FAB band is no longer dead space.
  expect(gap).toBeGreaterThanOrEqual(0)
  expect(gap).toBeLessThan(32)
})

test('mobile monitor: inject bar is inline, not a mid-screen sticky float', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'team_monitor_v2_terminal.yaml')])
  await page.goto('/monitor')

  const bar = page.locator('.bulk-inject-host')
  await expect(bar).toBeVisible()
  const position = await bar.evaluate((el) => getComputedStyle(el).position)
  // Sticky was what pinned it mid-screen above the agent stream.
  expect(position).toBe('static')
})

test('mobile approvals: opening a wide code preview does not scroll the page sideways', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'approvals_cron_pending.yaml')])
  await page.goto('/approvals')
  await page.locator('.approvals-row').first().click()
  await expect(page.locator('.approvals__detail-title')).toBeVisible()

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
  expect(overflow).toBeLessThanOrEqual(0)
})
