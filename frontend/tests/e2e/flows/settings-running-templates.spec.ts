/**
 * E2E flow: Settings → Running Templates (happy path).
 *
 * Drives the user-facing template-edit surface end-to-end:
 *   1. open Settings, switch to the Running Templates tab.
 *   2. session dropdown auto-selects the only active team.
 *   3. PM role auto-selected (first in `roles`); editor shows the live
 *      instruction.
 *   4. user edits the instruction → "● UNSAVED" pill appears.
 *   5. click Save → PATCH /template/roles/pm fires; history rail flips
 *      from "No edits yet." to a row keyed `pm.instruction`.
 *   6. click Rollback on that audit row → POST /rollback/101 fires; the
 *      editor refetches and the instruction is back to the original.
 *
 * Why this spec exists:
 *   PR #58 review flagged "Happy Path FIRST" as a hard requirement for
 *   user-facing changes. The destructive reload path is reachable from
 *   this tab — a broken click-flow kills running agents. This spec is
 *   the first line of defence against that.
 *
 * Out of scope (kept narrow on purpose):
 *   - Force-reload (destructive — covered separately if/when added)
 *   - Reset-to-yaml chooser modal (yaml is in_sync here)
 *   - View-diff modal (yaml is in_sync here)
 *   - server_overrides clear case (regression-covered by unit logic in
 *     _buildPatch; e2e for it would just duplicate the assertion)
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('happy path: pick session → edit role → save → audit appears → rollback', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'settings_running_templates.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/settings')

  // Switch to Running Templates tab. General is the default mount.
  await page.getByRole('button', { name: 'Running Templates', exact: true }).click()

  // Session dropdown auto-selects the only active team. Wait for the
  // template to render — PM role pill is the stable signal.
  await expect(page.locator('.role-item').filter({ hasText: 'PM' })).toBeVisible()
  await expect(page.locator('.role-item.active').filter({ hasText: 'PM' })).toBeVisible()

  // Editor shows the live instruction. <textarea> value isn't text content,
  // so hasText doesn't work — match by sibling label instead.
  const instructionEditor = page.locator(
    'label:has(span.fl:text("Instruction")) textarea',
  )
  await expect(instructionEditor).toHaveValue('You are the orchestrator.')

  // Initial state: history empty, no UNSAVED pill.
  await expect(page.getByText('No edits yet.')).toBeVisible()
  await expect(page.locator('.pill.dirty')).not.toBeVisible()

  // ── 1. Edit + Save ────────────────────────────────────────────────
  await instructionEditor.fill('You are the new orchestrator (edited via UI).')

  // Dirty pill appears once draft diverges from live.
  await expect(page.locator('.pill.dirty')).toBeVisible()

  const [patchResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'PATCH' &&
        new URL(r.url()).pathname ===
          '/api/team-sessions/agile-team_e2e0test/template/roles/pm',
    ),
    page.getByRole('button', { name: 'Save', exact: true }).click(),
  ])
  expect(patchResp.status()).toBe(200)

  // Assert the patch body contained only the changed field — the UI must
  // not splatter unchanged fields (audit log would otherwise be noisy).
  const patchCall = recorder.assertContains(
    'PATCH',
    '/api/team-sessions/agile-team_e2e0test/template/roles/pm',
  )
  const patchBody = patchCall.body as {
    patch?: Record<string, unknown>
    comment?: string
  } | null
  expect(patchBody?.patch).toEqual({
    instruction: 'You are the new orchestrator (edited via UI).',
  })
  expect(patchBody?.comment).toContain('Settings UI')

  // History rail flips to one row — pm.instruction edit.
  await expect(page.locator('.hist-row')).toHaveCount(1)
  await expect(page.locator('.hist-role').first()).toHaveText('pm.instruction')

  // After loadTemplate refetch, dirty pill clears.
  await expect(page.locator('.pill.dirty')).not.toBeVisible()

  // ── 2. Rollback ──────────────────────────────────────────────────
  // ConfirmModal pops up — assert title + click confirm.
  const rollbackPromise = page.waitForResponse(
    (r) =>
      r.request().method() === 'POST' &&
      new URL(r.url()).pathname ===
        '/api/team-sessions/agile-team_e2e0test/template/rollback/101',
  )
  await page.locator('.btn-mini').first().click()

  // Confirm dialog — the global ConfirmModal asks "Roll back this audit
  // row?". There's another button with text "Rollback" in the history
  // rail, so scope to the modal card to avoid a strict-mode collision.
  const modalCard = page.locator('.cm-card')
  await expect(
    modalCard.getByRole('heading', { name: /roll back this audit row/i }),
  ).toBeVisible()
  await modalCard.getByRole('button', { name: /^rollback$/i }).click()

  const rollbackResp = await rollbackPromise
  expect(rollbackResp.status()).toBe(200)

  // History grows to 2 rows; latest (rollback) on top.
  await expect(page.locator('.hist-row')).toHaveCount(2)
  await expect(page.locator('.hist-row.rollback').first()).toBeVisible()

  // Editor reflects the rollback — original instruction is back.
  await expect(instructionEditor).toHaveValue('You are the orchestrator.')

  // No unknown /api/* request leaked through — unknown requests would
  // be served as 599 by the mock backend, so we only fail-loud on those.
  // We deliberately skip assertAllFulfilled() because the merged noise
  // fixture declares paths (passkey, approvals stats) that an
  // authenticated user doesn't trigger, and other Settings specs don't
  // enforce strict consumption either.
  expect(backend.unexpected).toEqual([])
})

test('switching role with unsaved edits prompts to discard (bug_006)', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'settings_running_templates.yaml')])

  await page.goto('/settings')
  await page.getByRole('button', { name: 'Running Templates', exact: true }).click()
  await expect(page.locator('.role-item.active').filter({ hasText: 'PM' })).toBeVisible()

  const instructionEditor = page.locator(
    'label:has(span.fl:text("Instruction")) textarea',
  )
  await expect(instructionEditor).toHaveValue('You are the orchestrator.')

  // Make the role dirty, then try to switch to QE.
  await instructionEditor.fill('half-typed edit, not saved')
  await expect(page.locator('.pill.dirty')).toBeVisible()
  await page.locator('.role-item').filter({ hasText: 'QE' }).click()

  // A confirm dialog must appear — switching would otherwise silently
  // discard the edit.
  const modalCard = page.locator('.cm-card')
  await expect(
    modalCard.getByRole('heading', { name: /discard unsaved changes/i }),
  ).toBeVisible()

  // Cancel → stay on PM, edit + dirty pill preserved.
  await modalCard.locator('.cm-btn-cancel').click()
  await expect(page.locator('.role-item.active').filter({ hasText: 'PM' })).toBeVisible()
  await expect(instructionEditor).toHaveValue('half-typed edit, not saved')
  await expect(page.locator('.pill.dirty')).toBeVisible()

  // Try again, this time confirm → switch to QE, edit discarded.
  await page.locator('.role-item').filter({ hasText: 'QE' }).click()
  await modalCard.getByRole('button', { name: /discard & switch/i }).click()
  await expect(page.locator('.role-item.active').filter({ hasText: 'QE' })).toBeVisible()
  await expect(instructionEditor).toHaveValue('You are QE.')
})
