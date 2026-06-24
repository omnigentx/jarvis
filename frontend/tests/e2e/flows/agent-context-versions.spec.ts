/**
 * E2E: Context Versions tab — compaction visibility on AgentDetail.
 *
 * Contract under test:
 *   - The versions timeline lists compaction events with token savings
 *     (before → after, saved, %) and surfaces failed compactions inline.
 *   - Expanding a version lazy-loads the summary; "Before / After"
 *     lazy-loads the diff (metadata-first API — no large payloads until
 *     the user asks for them).
 *   - SSE lifecycle: context_compaction_completed shows a toast (visible
 *     feedback without interrupting the user); context_compaction_started
 *     drives the live "Compacting…" banner on the tab.
 *
 * Harness limitation (mock SSE bodies are atomic): the started→completed
 * sequence collapses instantly, so the live REFETCH-on-completion watch in
 * AgentDetail can't be observed end-to-end here — the toast + store state
 * transitions are asserted instead (store cases covered in
 * src/stores/agents.test.js).
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('versions timeline renders savings, summary and diff lazy-load', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'agent_context_versions.yaml'),
  ])

  await page.goto('/agents/Jarvis')
  await page.getByRole('button', { name: 'Context Versions' }).click()

  // Timeline: completed version shows tokens before → after + savings.
  await expect(page.getByText('✓ Compacted')).toBeVisible()
  await expect(page.getByText('10.0K → 4.0K')).toBeVisible()
  await expect(page.getByText('−6.0K (60%)')).toBeVisible()
  // Failed version surfaces its error inline.
  await expect(page.getByText('✕ Failed')).toBeVisible()
  await expect(page.getByText(/savings below minimum/).first()).toBeVisible()

  // Lazy-load check: neither detail nor diff fetched yet.
  const detailCalls = () =>
    backend.fulfilled.filter((c) => c.path.endsWith('/context/versions/42')).length
  const diffCalls = () =>
    backend.fulfilled.filter((c) => c.path.endsWith('/versions/42/diff')).length
  expect(detailCalls()).toBe(0)
  expect(diffCalls()).toBe(0)

  // Expand → summary loads with the marker + risks.
  await page.getByText('✓ Compacted').click()
  await expect(page.getByText('[COMPACTED_CONTEXT_SUMMARY]')).toBeVisible()
  await expect(page.getByText(/rule-based summary may miss/)).toBeVisible()
  await expect.poll(detailCalls).toBe(1)
  expect(diffCalls()).toBe(0)

  // Before / After → diff loads, dropped messages struck through,
  // summary message highlighted in the After column.
  await page.getByRole('button', { name: 'Before / After' }).click()
  await expect(page.getByText('Before (5 msgs)')).toBeVisible()
  await expect(page.getByText('After (4 msgs)')).toBeVisible()
  await expect.poll(diffCalls).toBe(1)
  await expect(page.locator('.version-diff-msg.diff-dropped')).toHaveCount(2)
  await expect(page.locator('.version-diff-msg.diff-summary')).toHaveCount(1)

  expect(page.url()).toContain('/agents/Jarvis')
})

test('context_compaction_completed SSE shows a savings toast', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'agent_context_versions.yaml'),
    join(FIXTURES, 'agent_context_versions_sse.yaml'),
  ])

  await page.goto('/agents/Jarvis')

  // Toast fires from the global activity stream (AppLayout) — bilingual
  // copy, so match either language.
  await expect(
    page.getByText(/Context compacted: Jarvis|Đã nén ngữ cảnh: Jarvis/),
  ).toBeVisible()
  await expect(page.getByText(/6,000|6\.000/).first()).toBeVisible()
})

test('context_compaction_started SSE drives the live banner on the tab', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'agent_context_versions.yaml'),
    join(FIXTURES, 'agent_context_versions_sse_started.yaml'),
  ])

  await page.goto('/agents/Jarvis')
  await page.getByRole('button', { name: 'Context Versions' }).click()

  await expect(page.getByText('Compacting context…')).toBeVisible()
})

test('versions fetch error shows error state, Retry recovers', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'agent_context_versions.yaml'),
  ])
  // First list request 500s; the retry falls through to the fixture.
  let calls = 0
  await page.route('**/api/agents/Jarvis/context/versions', (route) => {
    calls++
    if (calls === 1) {
      return route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'boom' }),
      })
    }
    return route.fallback()
  })

  await page.goto('/agents/Jarvis')
  await page.getByRole('button', { name: 'Context Versions' }).click()

  // Error state — must NOT read as "no compactions yet".
  await expect(page.getByText(/Failed to load compaction versions/)).toBeVisible()
  await expect(page.getByText(/No context compactions yet/)).toBeHidden()

  await page.getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByText('✓ Compacted')).toBeVisible()
})

test('versions tab renders the empty state', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'agent_context_versions.yaml'),
  ])
  // Override the list to be empty for this flow.
  await page.route('**/api/agents/Jarvis/context/versions', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ versions: [] }),
    }),
  )

  await page.goto('/agents/Jarvis')
  await page.getByRole('button', { name: 'Context Versions' }).click()

  await expect(page.getByText(/No context compactions yet/)).toBeVisible()
})
