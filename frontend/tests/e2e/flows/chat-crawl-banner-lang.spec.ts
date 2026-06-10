/**
 * E2E flow: crawl-progress banner copy follows the topbar Vi/En toggle LIVE.
 *
 * useLang() is a module-level shared ref — flipping the language in AppLayout
 * must re-render the ChatView banner (text + buttons) without a reload. A
 * stale banner here means the composable regressed to per-component state.
 *
 * Flow: send a chat message whose `done` event carries crawl_job_id →
 * banner appears in Vietnamese (default lang) → toggle topbar language →
 * same banner re-renders in English → toggle back → Vietnamese again.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('crawl banner flips Vi ↔ En live with the topbar language toggle', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'chat_crawl_banner_lang.yaml')])

  await page.goto('/chat')

  const textarea = page.getByPlaceholder(/type a message/i)
  await expect(textarea).toBeVisible()
  await textarea.fill('download this story please')
  await textarea.press('Enter')

  // `done` carries crawl_job_id → useCrawlStatus.track() polls status and the
  // banner renders in the default language (vi).
  const banner = page.locator('.crawl-strip')
  await expect(banner).toBeVisible()
  await expect(banner).toContainText('Đang tải "Test Story"')
  await expect(banner).toContainText('3/10 chương')
  await expect(banner.getByRole('button', { name: 'Huỷ' })).toBeVisible()

  // Toggle topbar language → SAME banner re-renders in English, no reload.
  const langToggle = page.locator('.topbar__icon-btn[title^="Language:"]')
  await langToggle.click()
  await expect(banner).toContainText('Downloading "Test Story"')
  await expect(banner).toContainText('3/10 chapters')
  await expect(banner.getByRole('button', { name: 'Cancel' })).toBeVisible()

  // And back — reactivity is two-way, not a one-shot mount read.
  await langToggle.click()
  await expect(banner).toContainText('Đang tải "Test Story"')
  await expect(banner.getByRole('button', { name: 'Huỷ' })).toBeVisible()
})
