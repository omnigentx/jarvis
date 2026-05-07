/**
 * MCP Servers view — catalog list, attach to agent, built-in protection.
 *
 * Cross-layer correctness invariants (DB rows, fast-agent aggregator state)
 * are covered by backend tests in tests/test_services/test_mcp_*.py. This
 * e2e suite is about the dashboard's UX and request shape — proving the UI
 * fires the right HTTP calls and renders the masked secrets, badges, and
 * attach matrix correctly.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')
const FIX = join(FIXTURES, 'mcp_servers.yaml')

test('MCP — list shows built-in + user servers with status indicators', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, FIX])

  await page.goto('/mcp-servers')

  // Two server rows.
  await expect(page.locator('.server-row')).toHaveCount(2)

  const githubRow = page.locator('.server-row').filter({ hasText: 'github' })
  const userRow = page.locator('.server-row').filter({ hasText: 'my-tool' })

  await expect(githubRow).toBeVisible()
  await expect(userRow).toBeVisible()

  // Built-in badge only on github.
  await expect(githubRow.locator('.badge-builtin')).toBeVisible()
  await expect(userRow.locator('.badge-builtin')).toHaveCount(0)

  // Status dots differ.
  await expect(githubRow.locator('.status-dot--running')).toBeVisible()
  await expect(userRow.locator('.status-dot--stopped')).toBeVisible()
})

test('MCP — secret env values are masked in detail panel', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, FIX])

  await page.goto('/mcp-servers')

  await page.locator('.server-row').filter({ hasText: 'github' }).click()

  // The masked value is rendered as ••••.
  const envBlock = page.locator('.env-list')
  await expect(envBlock).toContainText('GITHUB_PERSONAL_ACCESS_TOKEN')
  await expect(envBlock).toContainText('••••')
})

test('MCP — selecting a server lazy-loads tools from detail endpoint', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, FIX])

  await page.goto('/mcp-servers')

  // Tools fetched from /api/mcp/servers/github (list endpoint omits tools[]).
  const [detailResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'GET' &&
        new URL(r.url()).pathname === '/api/mcp/servers/github',
    ),
    page.locator('.server-row').filter({ hasText: 'github' }).click(),
  ])
  expect(detailResp.status()).toBe(200)

  const toolList = page.locator('.tool-list')
  await expect(toolList).toContainText('github-create_issue')
  await expect(toolList).toContainText('github-list_issues')
})

test('MCP — Delete is disabled for built-in server', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, FIX])

  await page.goto('/mcp-servers')

  await page.locator('.server-row').filter({ hasText: 'github' }).click()

  const deleteBtn = page.locator('.detail__actions').getByRole('button', { name: 'Delete' })
  await expect(deleteBtn).toBeDisabled()
})

test('MCP — Attach to agent via dropdown fires POST', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, FIX])

  await page.goto('/mcp-servers')

  await page.locator('.server-row').filter({ hasText: 'github' }).click()
  await page.locator('.tabs').getByRole('button', { name: /Agents/ }).click()

  // PersonalAgent is already attached → shown as detach pill.
  await expect(
    page.locator('.attached-row .link-detach').filter({ hasText: 'PersonalAgent' }),
  ).toBeVisible()

  // Open the "Attach to…" dropdown — only FinanceAgent should appear (PersonalAgent already attached).
  await page.getByRole('button', { name: 'Attach to…' }).click()
  const financeChoice = page.locator('.attach-item').filter({ hasText: 'FinanceAgent' })
  await expect(financeChoice).toBeVisible()

  const [postResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname === '/api/mcp/servers/github/agents/FinanceAgent',
    ),
    financeChoice.click(),
  ])
  expect(postResp.status()).toBe(200)
})

test('MCP — Detach pill on attached agent fires DELETE', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, FIX])

  await page.goto('/mcp-servers')

  await page.locator('.server-row').filter({ hasText: 'github' }).click()
  await page.locator('.tabs').getByRole('button', { name: /Agents/ }).click()

  const detachPill = page.locator('.attached-row .link-detach').filter({ hasText: 'PersonalAgent' })

  const [delResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'DELETE' &&
        new URL(r.url()).pathname === '/api/mcp/servers/github/agents/PersonalAgent',
    ),
    detachPill.click(),
  ])
  expect(delResp.status()).toBe(200)
})
