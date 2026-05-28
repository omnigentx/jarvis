/**
 * E2E flow: Team Monitor — terminal-style message_history view.
 *
 * Guards the rebuilt monitor pipeline: agent.message_history → SSE
 * `message_turn` channel → useAgentTurns composable → AgentTerminal
 * component. Coverage points:
 *
 *   1. The terminal grid renders on /monitor (no feature flag — v1
 *      card view was removed; AgentTerminal is the only UI).
 *   2. Initial GET /messages populates the terminal with persisted turns.
 *   3. SSE message_turn appends a delta turn live (no duplicate).
 *   4. Truncated content shows a "Show full" affordance; clicking it
 *      triggers /turns/{idx}/full and reveals the untruncated text.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')


test('team monitor v2 — terminal renders persisted history + SSE delta turn', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'team_monitor_v2_terminal.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  // No feature flag — terminal UI is the only monitor view since the
  // v1 card grid was removed.
  await page.goto('/monitor')

  // ── 1. Terminal grid renders ────────────────────────────────────────
  // The .agent-terminal class is unique to AgentTerminal.vue.
  await expect(page.locator('.agent-terminal')).toHaveCount(1)
  await expect(page.locator('.agent-name', { hasText: 'Jarvis' })).toBeVisible()

  // ── 2. Initial history fetched and rendered ────────────────────────
  // Two turns from /messages: turn_idx 0 (user) + turn_idx 1 (assistant).
  // Then the SSE delta adds turn_idx 2.
  await expect(page.locator('.turn-row')).toHaveCount(3)

  // Initial user turn text visible.
  await expect(
    page.locator('.turn-row.role-user', { hasText: 'thời tiết Gia Lâm hôm nay' }),
  ).toBeVisible()

  // ── 3. SSE delta turn appears (turn_idx=2 with a tool_call) ────────
  const deltaRow = page.locator('.turn-row.role-assistant', {
    hasText: 'Hôm nay Gia Lâm 28°C',
  })
  await expect(deltaRow).toBeVisible()
  await expect(deltaRow.locator('.tool-name')).toHaveText('search_weather')

  // ── 4. Mount-time fetches fired — initial /messages is the v2 contract ──
  recorder.assertContains('GET', '/api/agents')
  recorder.assertContains('GET', '/api/agents/Jarvis/messages')

  // No unexpected requests slipped past the mock.
  expect(backend.unexpected.length).toBe(0)
})


test('team monitor v2 — Show full expands truncated assistant turn', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'team_monitor_v2_terminal.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/monitor')

  // Wait for the assistant turn (turn_idx=1) carrying the truncated block.
  const truncatedRow = page.locator('.turn-row.role-assistant', {
    hasText: 'Tôi đang tra cứu thời tiết',
  }).first()
  await expect(truncatedRow).toBeVisible()

  // The "+ Show full" button is the affordance for truncated content.
  const expandBtn = truncatedRow.locator('button.expand-btn', { hasText: 'Show full' })
  await expect(expandBtn).toBeVisible()

  // Click → triggers GET /turns/1/full and shows the untruncated text.
  await expandBtn.click()

  // Wait for the full-content endpoint to be called.
  await expect.poll(() => {
    try { recorder.assertContains('GET', '/api/agents/Jarvis/turns/1/full'); return true }
    catch { return false }
  }).toBe(true)

  // The full text from the fixture appears.
  await expect(truncatedRow).toContainText('FULL CONTENT REVEALED HERE')

  // The button label flips to "Collapse".
  await expect(truncatedRow.locator('button.expand-btn')).toHaveText('Collapse')

  expect(backend.unexpected.length).toBe(0)
})


// Removed: "version toggle persists choice" test.
//
// The TeamMonitor v1/v2 toggle was deleted (see the dashboard commit
// "TeamMonitor terminal UI..."). The terminal grid is now the only
// monitor view — there is nothing to toggle and no localStorage flag
// to persist. Keeping a test for non-existent UI would be a
// false-positive regression source.
