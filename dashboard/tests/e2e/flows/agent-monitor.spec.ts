/**
 * E2E flow: Team Monitor realtime agent status.
 *
 * Guards the "did my agent spawn? is it still running?" dashboard. On mount
 * the view pulls the agent roster + persisted activity history, then relies
 * on the /api/agents/activity-stream SSE pipeline to mutate status badges
 * in realtime through stores/agents.js::processEvent.
 *
 * Coverage:
 *  1. Happy path — two agents render as cards and an SSE `idle` event flips
 *     one card's status badge from Running to Idle (proves SSE → store →
 *     DOM wiring is intact).
 *  2. Negative control — empty roster renders the empty-state placeholder
 *     without spurious fetches.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('team monitor — roster renders and SSE event flips one agent status', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'agent_monitor_live.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/monitor')

  // Assertion 1: both agents render as cards. Anchor on the stable count
  // first — the grid hydrates from /api/agents and any assertion targeting
  // a specific card before then would race.
  await expect(page.locator('.agent-panel')).toHaveCount(2)

  const alphaCard = page.locator('.agent-panel').filter({ hasText: 'alpha-agent' })
  const betaCard = page.locator('.agent-panel').filter({ hasText: 'beta-agent' })
  await expect(alphaCard).toBeVisible()
  await expect(betaCard).toBeVisible()

  // Assertion 2: header title confirms we're on the Team Monitor.
  await expect(page.getByRole('heading', { name: 'Team Monitor' })).toBeVisible()

  // Assertion 3: SSE event flips alpha-agent to Idle.
  // Initially alpha is "running" (from GET /api/agents); the SSE event
  // processed through stores/agents.js::processEvent mutates status to "idle".
  // Beta is seeded as "idle" and should stay "idle".
  await expect(alphaCard.locator('.panel-status-badge')).toHaveText('Idle')
  await expect(betaCard.locator('.panel-status-badge')).toHaveText('Idle')

  // Assertion 4: mount-time fetches fired.
  recorder.assertContains('GET', '/api/agents')
  recorder.assertContains('GET', '/api/agents/activities/recent')

  // Assertion 5: no request hit the mock without a fixture match.
  expect(backend.unexpected.length).toBe(0)
})

test('team monitor — empty roster renders empty-state placeholder', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'agent_monitor_empty.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/monitor')

  // Assertion 1: Team Monitor heading is visible — proves we reached the view.
  await expect(page.getByRole('heading', { name: 'Team Monitor' })).toBeVisible()

  // Assertion 2: empty-state placeholder renders (default filter is 'all').
  await expect(page.locator('.empty-state')).toContainText('No agents found')

  // Assertion 3: no agent card rendered.
  await expect(page.locator('.agent-panel')).toHaveCount(0)

  // Assertion 4: boot-time fetches still fired.
  recorder.assertContains('GET', '/api/agents')
  recorder.assertContains('GET', '/api/agents/activities/recent')

  // Assertion 5: no unexpected requests.
  expect(backend.unexpected.length).toBe(0)
})
