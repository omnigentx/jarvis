/**
 * E2E flow: Token Usage dashboard (/token-usage).
 *
 * Financial-visibility view — regressions here hide LLM spending.
 *
 * Coverage:
 *  1. Populated view: mount fetches GET /api/metrics/tokens?period=24h,
 *     totals render formatted ("1.2M", "$12.34"), each agent row renders
 *     with its formatted cost, period defaults to 24H.
 *  2. Negative control — empty state: zero totals + empty arrays render the
 *     "No token data" / "No token usage recorded" copy without crashing, and
 *     no stray requests fire (catches a refactor that polls in a loop).
 *  3. Period filter: clicking the 7D tab triggers a SECOND fetch with
 *     ?period=7d in the query string.  Guards the `watch(period)` re-fetch.
 *
 * Structure mirrors flows/setup-wizard.spec.ts.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('populated view renders totals, per-agent rows, and formatted cost', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'token_usage_with_data.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/token-usage')

  // Header landmark — confirms the view rendered at all.
  await expect(page.getByRole('heading', { name: 'Token Usage' })).toBeVisible()

  // Metric cards: pinned formatted strings from fmtTokens / fmtCost / fmtPercent.
  // Picking exact-value getByText so we catch a formatter regression, not just
  // "some number shows up".
  //   totals.total_tokens = 1_234_500  → "1.2M"
  //   totals.input_tokens = 900_000    → "900.0K"
  //   totals.output_tokens = 334_500   → "334.5K"
  //   totals.est_cost = 12.34          → "$12.34"
  //   cache hit rate = 100_000/900_000 → "11.1%"
  await expect(page.getByText('1.2M', { exact: true })).toBeVisible()
  await expect(page.getByText('900.0K', { exact: true })).toBeVisible()
  await expect(page.getByText('334.5K', { exact: true })).toBeVisible()
  await expect(page.getByText('$12.34', { exact: true })).toBeVisible()
  await expect(page.getByText('11.1%', { exact: true })).toBeVisible()
  // "42 LLM calls" shows as the sub-label of the Total Tokens card.
  await expect(page.getByText('42 LLM calls')).toBeVisible()

  // Agent names appear twice on the page (once in the bar chart, once in the
  // breakdown table) — assert both occurrences so a regression that drops one
  // panel still fails the test.
  await expect(page.getByText('Jarvis', { exact: true })).toHaveCount(2)
  await expect(page.getByText('MusicAgent', { exact: true })).toHaveCount(2)
  await expect(page.getByText('CrawlAgent', { exact: true })).toHaveCount(2)

  // Row-level cost formatting — each agent's est_cost rendered with $ prefix
  // and 2 decimals by fmtCost (since all values are >= 0.01).
  await expect(page.getByText('$8.20', { exact: true })).toBeVisible()
  await expect(page.getByText('$3.10', { exact: true })).toBeVisible()
  await expect(page.getByText('$1.04', { exact: true })).toBeVisible()

  // Default period tab is 24H, and table footer counts the rows.
  await expect(page.getByText('3 agents')).toBeVisible()

  // Model breakdown renders both models.
  await expect(page.getByText('gpt-4o-mini')).toBeVisible()
  await expect(page.getByText('claude-3-5-sonnet')).toBeVisible()

  // Contract: TokenUsage.vue fetches /api/metrics/tokens with period=24h on
  // mount.  Assert the call fired AND the query param is what the UI claims.
  const metricsCall = recorder.assertContains('GET', '/api/metrics/tokens')
  expect(metricsCall.query.period).toBe('24h')

  expect(backend.unexpected.length).toBe(0)
})

test('negative control: empty state renders without crashing or extra fetches', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'token_usage_empty.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/token-usage')

  // Page header still renders — view doesn't bail out on zero data.
  await expect(page.getByRole('heading', { name: 'Token Usage' })).toBeVisible()

  // Empty-state copy from TokenUsage.vue — these strings are user-visible
  // signals that "we have no data" vs. "we failed silently".
  await expect(page.getByText('No token data for this period')).toBeVisible()
  await expect(
    page.getByText('No token usage recorded for this period.')
  ).toBeVisible()
  await expect(page.getByText('No data', { exact: true })).toBeVisible()

  // Zero-formatted cost from fmtCost(0) === "$0.00".  If the view mis-handled
  // a missing totals field it would render "$NaN" — this pins the happy path.
  await expect(page.getByText('$0.00').first()).toBeVisible()

  // Table row count reads "0 agents" (plural branch of `length !== 1`).
  await expect(page.getByText('0 agents')).toBeVisible()

  // Exactly ONE metrics fetch — the view must not poll or refetch in a loop.
  const metricsCalls = recorder.calls.filter(
    (c) => c.method === 'GET' && c.path === '/api/metrics/tokens'
  )
  expect(metricsCalls).toHaveLength(1)
  expect(metricsCalls[0].query.period).toBe('24h')

  expect(backend.unexpected.length).toBe(0)
})

test('changing the period tab re-fetches with the new period query param', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'token_usage_with_data.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/token-usage')

  // Wait for the initial fetch to land (header + one of the pinned values)
  // before simulating the user click — otherwise the recorder might see only
  // the second fetch on a fast test runner.
  await expect(page.getByText('1.2M', { exact: true })).toBeVisible()
  await expect.poll(() =>
    recorder.calls.filter(
      (c) => c.method === 'GET' && c.path === '/api/metrics/tokens'
    ).length
  ).toBe(1)

  // Click the 7D period tab — label comes from the `periods` array in the view.
  await page.getByRole('button', { name: '7D', exact: true }).click()

  // Assert the second fetch fires with ?period=7d.  `watch(period, fetchMetrics)`
  // is the contract this click exercises.
  await expect.poll(() =>
    recorder.calls.filter(
      (c) => c.method === 'GET' && c.path === '/api/metrics/tokens'
    ).length
  ).toBe(2)

  const metricsCalls = recorder.calls.filter(
    (c) => c.method === 'GET' && c.path === '/api/metrics/tokens'
  )
  expect(metricsCalls[0].query.period).toBe('24h')
  expect(metricsCalls[1].query.period).toBe('7d')

  expect(backend.unexpected.length).toBe(0)
})
