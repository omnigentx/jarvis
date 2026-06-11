/**
 * E2E: conversation sidebar management — bulk delete, per-agent scoping,
 * infinite-scroll paging.
 *
 * Backend changes under test:
 *   GET  /api/conversations?agent_name=&limit=&offset=  → { items, total }
 *   POST /api/conversations/bulk-delete { ids }          → { deleted, failed }
 *
 * The mock matches method+path only (query ignored), so these assert the
 * client wiring — agent_name in the query, ids in the bulk body, a second
 * page request on scroll — while server-side filtering/paging is covered by
 * backend unit tests (tests/test_routes/test_sessions.py).
 */
import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('bulk delete: select two conversations and remove them in one request', async ({
  page,
}) => {
  await seedApiKey(page)
  const recorder = new NetworkRecorder()
  await recorder.attach(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'conversations_bulk.yaml')])

  await page.goto('/chat')
  await expect(page.locator('.conv-item')).toHaveCount(3)

  // Enter select mode, tick Alpha + Beta.
  await page.locator('[data-testid="conv-select-toggle"]').click()
  await page.locator('.conv-item', { hasText: 'Alpha conversation' }).click()
  await page.locator('.conv-item', { hasText: 'Beta conversation' }).click()

  const bulkBtn = page.locator('[data-testid="conv-bulk-delete"]')
  await expect(bulkBtn).toBeEnabled()
  await expect(page.locator('.conv-bulk-count')).toHaveText('2 selected')

  // Confirm the modal.
  await bulkBtn.click()
  await page.locator('.cm-btn-variant').click()

  // Only Gamma survives, and exactly one batch request carried both ids.
  await expect(page.locator('.conv-item')).toHaveCount(1)
  await expect(page.locator('.conv-item')).toContainText('Gamma conversation')

  const bulk = recorder.calls.filter(
    (c) => c.method === 'POST' && c.path === '/api/conversations/bulk-delete',
  )
  expect(bulk).toHaveLength(1)
  const ids = (bulk[0].body as { ids: string[] }).ids
  expect(new Set(ids)).toEqual(new Set(['conv-a', 'conv-b']))
})

test('per-agent scoping: switching agent refetches that agent’s conversations', async ({
  page,
}) => {
  await seedApiKey(page)
  const recorder = new NetworkRecorder()
  await recorder.attach(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'conversations_bulk.yaml')])

  await page.goto('/chat')
  await expect(page.locator('.conv-item')).toHaveCount(3)
  // Initial load scopes to the default agent.
  await expect
    .poll(() =>
      recorder.calls.some(
        (c) => c.path === '/api/conversations' && c.query.agent_name === 'Jarvis',
      ),
    )
    .toBe(true)

  // Switch to IoT via the header dropdown.
  await page.locator('.hd-switch-btn').click()
  await page.locator('.hd-dropdown-item', { hasText: 'IoT' }).click()

  // A fresh list request fires, scoped to the new agent.
  await expect
    .poll(() =>
      recorder.calls.some(
        (c) => c.path === '/api/conversations' && c.query.agent_name === 'IoT',
      ),
    )
    .toBe(true)
})

test('infinite scroll: a second page is requested when more remain', async ({
  page,
}) => {
  await seedApiKey(page)
  const recorder = new NetworkRecorder()
  await recorder.attach(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'conversations_paged.yaml')])

  await page.goto('/chat')
  await expect(page.locator('.conv-item')).toHaveCount(3)

  // total (8) > loaded (3) → the sentinel triggers a follow-up page fetch.
  await expect
    .poll(
      () =>
        recorder.calls.filter((c) => c.path === '/api/conversations').length,
      { timeout: 5000 },
    )
    .toBeGreaterThanOrEqual(2)

  // Deduped by id — no duplicate rows despite the mock returning the same page.
  await expect(page.locator('.conv-item')).toHaveCount(3)
})
