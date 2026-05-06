/**
 * Skills Library page + attach/detach flows.
 *
 * Coverage:
 *   • Library lists every skill (built-in + user, attached + orphan).
 *   • Filters: All, Built-in, User-created, Orphan.
 *   • Attach to a card-based agent → persisted=true, no warning.
 *   • Attach to a code-based agent → window.confirm warning fires; on
 *     accept, request goes through and toast says "runtime only".
 *   • Detach from a card-based agent works.
 *   • Delete button disabled for built-in skills.
 *
 * The runtime-correctness invariants ("agent.instruction actually changes")
 * are covered by the BACKEND integration tests in
 * tests/test_services/test_skill_service_integration.py — that's where the
 * real fast-agent rebuild_agent_instruction is exercised. These e2e tests
 * verify the dashboard's UX, request shape, and post-mutation refresh.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('Library — lists all skills with badges and used-by chips', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'skills_library.yaml')])

  await page.goto('/skills')

  // Four rows visible.
  await expect(page.locator('.skill-row')).toHaveCount(4)

  // Built-in badge only on built-in skills.
  await expect(
    page.locator('.skill-row').filter({ hasText: 'user-context' }).locator('.badge-builtin')
  ).toBeVisible()
  await expect(
    page.locator('.skill-row').filter({ hasText: 'orphan-skill' }).locator('.badge-builtin')
  ).toHaveCount(0)

  // Used-by text on the attached skills.
  await expect(
    page.locator('.skill-row').filter({ hasText: 'my-attached' }).locator('.used-by')
  ).toContainText('FinanceAgent')
  await expect(
    page.locator('.skill-row').filter({ hasText: 'orphan-skill' }).locator('.used-by')
  ).toContainText('Not attached')
})

test('Library — Orphan filter shows only unattached skills', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'skills_library.yaml')])

  await page.goto('/skills')

  await page.getByRole('button', { name: 'Orphan', exact: true }).click()
  // Only audio-reading + orphan-skill remain.
  await expect(page.locator('.skill-row')).toHaveCount(2)
  await expect(page.locator('.skill-row').filter({ hasText: 'orphan-skill' })).toBeVisible()
  await expect(page.locator('.skill-row').filter({ hasText: 'audio-reading' })).toBeVisible()
})

test('Library — Delete button disabled for built-in skills', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'skills_library.yaml')])

  await page.goto('/skills')

  const builtinRow = page.locator('.skill-row').filter({ hasText: 'user-context' })
  const userRow = page.locator('.skill-row').filter({ hasText: 'orphan-skill' })

  // Delete is the second icon button per row (Edit then Delete).
  await expect(builtinRow.locator('.icon-btn-danger')).toBeDisabled()
  await expect(userRow.locator('.icon-btn-danger')).toBeEnabled()
})

test('Library — attach orphan to card-based agent fires PUT and refreshes', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'skills_library.yaml')])

  await page.goto('/skills')

  const orphanRow = page.locator('.skill-row').filter({ hasText: 'orphan-skill' })
  await orphanRow.getByRole('button', { name: 'Attach to…' }).click()

  // FinanceAgent is card-based — no "runtime only" pill on the row.
  const financeChoice = page.locator('.attach-item').filter({ hasText: 'FinanceAgent' })
  await expect(financeChoice).toBeVisible()
  await expect(financeChoice.locator('.attach-warn-pill')).toHaveCount(0)

  const [putResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'PUT' &&
        new URL(r.url()).pathname === '/api/skills/orphan-skill/agents/FinanceAgent',
    ),
    financeChoice.click(),
  ])
  expect(putResp.status()).toBe(200)
  // No body required for attach; just the URL identifies the relationship.

  // Toast says attached — and "runtime only" hint is NOT in the message.
  const toast = page.locator('.toast')
  await expect(toast).toBeVisible()
  await expect(toast).not.toContainText(/runtime only/i)
})

test('Library — attach to code-based agent shows runtime-only warning + confirm', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'skills_library.yaml')])

  await page.goto('/skills')

  const orphanRow = page.locator('.skill-row').filter({ hasText: 'orphan-skill' })
  await orphanRow.getByRole('button', { name: 'Attach to…' }).click()

  // Jarvis is code-based — must carry the "runtime only" pill in the menu.
  const jarvisChoice = page.locator('.attach-item').filter({ hasText: 'Jarvis' })
  await expect(jarvisChoice).toBeVisible()
  await expect(jarvisChoice.locator('.attach-warn-pill')).toBeVisible()

  await jarvisChoice.click()

  // The themed in-app confirm dialog must surface — not the browser-native
  // window.confirm. Verify the message body covers the runtime/agent.py
  // consequence so the user understood what they were accepting.
  const dialog = page.locator('.cm-card')
  await expect(dialog).toBeVisible()
  await expect(dialog.locator('.cm-desc')).toContainText(/runtime/i)
  await expect(dialog.locator('.cm-desc')).toContainText(/agent\.py/i)

  const [putResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'PUT' &&
        new URL(r.url()).pathname === '/api/skills/orphan-skill/agents/Jarvis',
    ),
    dialog.getByRole('button', { name: /Attach/ }).click(),
  ])
  expect(putResp.status()).toBe(200)

  // Toast surfaces persistence state — backend returned persisted=false, so
  // the user-facing message says "runtime only".
  await expect(page.locator('.toast')).toContainText(/runtime only/i)
})

test('Library — detach via the per-agent chip on the skill row', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'skills_library.yaml')])

  await page.goto('/skills')

  const myRow = page.locator('.skill-row').filter({ hasText: 'my-attached' })
  const chip = myRow.getByRole('button', { name: '× FinanceAgent' })
  await expect(chip).toBeVisible()
  await chip.click()

  // Themed confirm dialog — accept it.
  const dialog = page.locator('.cm-card')
  await expect(dialog).toBeVisible()

  const [delResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'DELETE' &&
        new URL(r.url()).pathname === '/api/skills/my-attached/agents/FinanceAgent',
    ),
    dialog.getByRole('button', { name: /Detach/ }).click(),
  ])
  expect(delResp.status()).toBe(200)
})
