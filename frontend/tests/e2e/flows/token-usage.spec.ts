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

  // Header landmark — confirms the view rendered at all.  The frontend h1 is
  // the live summary "{tokens} tokens · {cost} · last {period} · live" (no
  // literal "Token Usage" string), so match the populated summary.
  await expect(
    page.getByRole('heading', { name: '1.2M tokens · $12.34 · last 24h · live' })
  ).toBeVisible()

  // Metric cards: pinned formatted strings from fmtTokens / fmtCost / fmtPercent.
  // Picking exact-value getByText so we catch a formatter regression, not just
  // "some number shows up".
  //   totals.total_tokens = 1_234_500  → "1.2M"  (also appears in the h1)
  //   totals.input_tokens = 900_000    → "900.0k" (fmtTokens uses lowercase k)
  //   totals.output_tokens = 334_500   → "334.5k"
  //   totals.est_cost = 12.34          → "$12.34"
  //   cache hit rate = 100_000/900_000 → "11.1%"
  // "1.2M" renders both in the h1 grad span and the KPI card, so use .first().
  await expect(page.getByText('1.2M', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('900.0k', { exact: true })).toBeVisible()
  await expect(page.getByText('334.5k', { exact: true })).toBeVisible()
  await expect(page.getByText('$12.34', { exact: true })).toBeVisible()
  await expect(page.getByText('11.1%', { exact: true })).toBeVisible()
  // "42 llm calls" shows as the sub-label of the Total Tokens card (lowercase).
  await expect(page.getByText('42 llm calls')).toBeVisible()

  // Agent names appear twice within the tokens view (once in the bar chart,
  // once in the breakdown table) — assert both occurrences so a regression
  // that drops one panel still fails the test.  Scoped to `.tokens` so the
  // sidebar "Jarvis" workspace label doesn't inflate the count; non-exact
  // match because the breakdown row-name span wraps a "{n} calls" badge child.
  const tokensView = page.locator('.tokens')
  await expect(tokensView.getByText('Jarvis')).toHaveCount(2)
  await expect(tokensView.getByText('MusicAgent')).toHaveCount(2)
  await expect(tokensView.getByText('CrawlAgent')).toHaveCount(2)

  // Row-level cost formatting — each agent's est_cost rendered with $ prefix
  // and 2 decimals by fmtCost (since all values are >= 0.01).
  await expect(page.getByText('$8.20', { exact: true })).toBeVisible()
  await expect(page.getByText('$3.10', { exact: true })).toBeVisible()
  await expect(page.getByText('$1.04', { exact: true })).toBeVisible()

  // Default period tab is 24H (frontend has no "N agents" footer; the row
  // count is verified via the three per-agent bars in the distribution chart).
  await expect(page.locator('.tokens__bar-row')).toHaveCount(3)

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

  // Page header still renders — view doesn't bail out on zero data.  Empty-state
  // h1 is the zero summary "0 tokens · $0.00 · last 24h · live".
  await expect(
    page.getByRole('heading', { name: '0 tokens · $0.00 · last 24h · live' })
  ).toBeVisible()

  // Empty-state copy from TokenUsage.vue — these strings are user-visible
  // signals that "we have no data" vs. "we failed silently".
  await expect(page.getByText('No token data for this period')).toBeVisible()
  await expect(
    page.getByText('No agent usage for this period.')
  ).toBeVisible()
  await expect(page.getByText('No data', { exact: true })).toBeVisible()

  // Zero-formatted cost from fmtCost(0) === "$0.00".  If the view mis-handled
  // a missing totals field it would render "$NaN" — this pins the happy path.
  await expect(page.getByText('$0.00').first()).toBeVisible()

  // No per-agent bars render (frontend has no "N agents" footer; zero rows is
  // the observable empty-state signal).
  await expect(page.locator('.tokens__bar-row')).toHaveCount(0)

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
  await expect(page.getByText('1.2M', { exact: true }).first()).toBeVisible()
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
