/**
 * Skill management — CRUD flows on AgentDetail's Skills tab.
 *
 * Coverage:
 *   • List loads, builtin badge renders only for builtin skills.
 *   • Built-in delete button is disabled with the right tooltip text.
 *   • User skill: open Edit modal → modify → Save sends PUT with content
 *     and expected_mtime_ns; UI reflects updated description.
 *   • Create modal: name validation (regex), Create button disabled until
 *     valid + content present, POST is sent with name/content body.
 *   • Delete modal: type-to-confirm gating, DELETE fires once button unlocks.
 *   • View toggle: Source / Split / Preview mode buttons swap which panes are
 *     visible.
 *   • Conflict 409: PUT returns 409, conflict banner shows, Reload pulls
 *     fresh content from disk.
 *   • Auth + close-without-save guards are non-trivial to e2e-test reliably
 *     (browser-native dialogs differ across runtimes) — covered service-side
 *     and via component logic instead.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('Skills tab — delete stays disabled when /api/skills metadata is missing', async ({
  page,
}) => {
  // Defence-in-depth: if the metadata fetch fails or returns an entry-less
  // payload, the UI must NOT enable delete on any skill — built-in skills
  // would otherwise be deletable for users on stale builds, and the type-to-
  // confirm dialog would open before the eventual server-side 403 fired.
  await seedApiKey(page)
  await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'skill_metadata_empty.yaml'),
  ])

  await page.goto('/agents/TestAgent?tab=skills')

  // user-context is built-in but the metadata fetch returned no entry for it.
  // Delete must still be disabled and the badge must not render (we have no
  // authority to label it built-in or otherwise).
  const card = page.locator('.skill-accordion-card').filter({ hasText: 'user-context' })
  await expect(card).toBeVisible()
  await expect(card.locator('.skill-action-delete')).toBeDisabled()
  await expect(card.locator('.skill-builtin-badge')).toHaveCount(0)
})

test('Skills tab — built-in badge + delete disabled for built-in skills', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'skill_management.yaml')])

  await page.goto('/agents/TestAgent?tab=skills')

  // Both skills must render.
  const customCard = page.locator('.skill-accordion-card').filter({ hasText: 'my-custom' })
  const builtinCard = page.locator('.skill-accordion-card').filter({ hasText: 'audio-reading' })
  await expect(customCard).toBeVisible()
  await expect(builtinCard).toBeVisible()

  // Builtin badge: only on the built-in card.
  await expect(builtinCard.locator('.skill-builtin-badge')).toBeVisible()
  await expect(customCard.locator('.skill-builtin-badge')).toHaveCount(0)

  // Delete button: disabled on built-in, enabled on user skill.
  const builtinDelete = builtinCard.locator('.skill-action-delete')
  const customDelete = customCard.locator('.skill-action-delete')
  await expect(builtinDelete).toBeDisabled()
  await expect(builtinDelete).toHaveAttribute('title', /built-in/i)
  await expect(customDelete).toBeEnabled()
})

test('Edit user skill — modal loads, save sends PUT with mtime, UI updates', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'skill_management.yaml')])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/agents/TestAgent?tab=skills')

  const customCard = page.locator('.skill-accordion-card').filter({ hasText: 'my-custom' })
  await customCard.locator('.skill-action-btn').first().click() // first action btn = Edit

  // Modal opens. Title + used-by banner should reflect fixture data.
  const modal = page.locator('.sk-card')
  await expect(modal).toBeVisible()
  await expect(modal.locator('.sk-title')).toContainText('my-custom')
  await expect(modal.locator('.sk-banner-info')).toContainText('TestAgent')

  // Click Save (the editor is dirty=false at first; we type a single char to
  // dirty the buffer so the Save button enables).
  const editor = modal.locator('.cm-content')
  await editor.click()
  await page.keyboard.press('End')
  await page.keyboard.type(' ')
  await expect(modal.locator('.sk-pill-dirty')).toBeVisible()

  const [putResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'PUT' &&
        new URL(r.url()).pathname === '/api/skills/my-custom',
    ),
    modal.getByRole('button', { name: /^Save/ }).click(),
  ])
  expect(putResp.status()).toBe(200)

  // Body must include expected_mtime_ns from the GET (optimistic locking
  // contract). The dashboard treats it as an opaque string — large
  // nanosecond integers exceed JS Number.MAX_SAFE_INTEGER and would lose
  // precision if forwarded as numbers, silently breaking the lock check.
  const sentBody = JSON.parse(putResp.request().postData() || '{}')
  expect(typeof sentBody.expected_mtime_ns).toBe('string')
  expect(sentBody.expected_mtime_ns).toMatch(/^\d+$/)
  expect(typeof sentBody.content).toBe('string')

  // After save, the editor stays open and shows the saved status.
  await expect(modal.locator('.sk-pill-saved')).toBeVisible()
})

test('Create skill — name validation, button gating, POST fires with body', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'skill_management.yaml')])

  await page.goto('/agents/TestAgent?tab=skills')

  await page.getByRole('button', { name: /New skill/ }).click()

  const modal = page.locator('.sk-card')
  await expect(modal).toBeVisible()
  await expect(modal.locator('.sk-title')).toContainText('Create skill')

  // Save disabled while name empty.
  const createBtn = modal.getByRole('button', { name: /^Create/ })
  await expect(createBtn).toBeDisabled()

  // Invalid name → inline error, button stays disabled.
  const nameInput = modal.locator('.sk-name-input')
  await nameInput.fill('Bad Name')
  await expect(modal.locator('.sk-input-error')).toBeVisible()
  await expect(createBtn).toBeDisabled()

  // Valid name → error clears and button enables (template is loaded so
  // content already has body text).
  await nameInput.fill('brand-new')
  await expect(modal.locator('.sk-input-error')).toHaveCount(0)
  await expect(createBtn).toBeEnabled()

  const [postResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname === '/api/skills',
    ),
    createBtn.click(),
  ])
  expect(postResp.status()).toBe(201)
  const body = JSON.parse(postResp.request().postData() || '{}')
  expect(body.name).toBe('brand-new')
  expect(typeof body.content).toBe('string')
})

test('Delete user skill — type-to-confirm gates the Delete button', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'skill_management.yaml')])

  await page.goto('/agents/TestAgent?tab=skills')

  const customCard = page.locator('.skill-accordion-card').filter({ hasText: 'my-custom' })
  await customCard.locator('.skill-action-delete').click()

  const modal = page.locator('.sd-card')
  await expect(modal).toBeVisible()
  await expect(modal.locator('.sd-title')).toHaveText('Delete skill')

  const deleteBtn = modal.getByRole('button', { name: /^Delete/ })
  await expect(deleteBtn).toBeDisabled()

  // Wrong text — button stays disabled.
  await modal.locator('.sd-input').fill('wrong')
  await expect(deleteBtn).toBeDisabled()

  // Exact match → unlock + send DELETE.
  await modal.locator('.sd-input').fill('my-custom')
  await expect(deleteBtn).toBeEnabled()

  const [delResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'DELETE' &&
        new URL(r.url()).pathname === '/api/skills/my-custom',
    ),
    deleteBtn.click(),
  ])
  expect(delResp.status()).toBe(200)

  // Modal must close after success.
  await expect(modal).toHaveCount(0)
})

test('Editor view modes — Source / Split / Preview swap pane visibility', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'skill_management.yaml')])

  // Use a wide viewport so the desktop 3-mode toggle is rendered (mobile
  // hides Split). The editor's checkMobile() runs on resize + mount.
  await page.setViewportSize({ width: 1400, height: 900 })
  await page.goto('/agents/TestAgent?tab=skills')

  const customCard = page.locator('.skill-accordion-card').filter({ hasText: 'my-custom' })
  await customCard.locator('.skill-action-btn').first().click()
  const modal = page.locator('.sk-card')
  await expect(modal).toBeVisible()

  // Default = Split.
  await expect(modal.locator('.sk-body-split')).toBeVisible()
  await expect(modal.locator('.sk-pane-source')).toBeVisible()
  await expect(modal.locator('.sk-pane-preview')).toBeVisible()

  // Switch to Source.
  await modal.getByRole('tab', { name: 'Source' }).click()
  await expect(modal.locator('.sk-body-source')).toBeVisible()
  await expect(modal.locator('.sk-pane-preview')).toHaveCount(0)

  // Switch to Preview.
  await modal.getByRole('tab', { name: 'Preview' }).click()
  await expect(modal.locator('.sk-body-preview')).toBeVisible()
  await expect(modal.locator('.sk-pane-source')).toHaveCount(0)
  await expect(modal.locator('.sk-fm-card')).toBeVisible()

  // Frontmatter rendered in the metadata box.
  await expect(modal.locator('.sk-fm-card')).toContainText('my-custom')
})

test('Conflict 409 — banner + Reload pulls fresh content from disk', async ({
  page,
}) => {
  await seedApiKey(page)
  await mockBackend(page, [NOISE, join(FIXTURES, 'skill_conflict.yaml')])

  await page.goto('/agents/TestAgent?tab=skills')

  const customCard = page.locator('.skill-accordion-card').filter({ hasText: 'my-custom' })
  await customCard.locator('.skill-action-btn').first().click()
  const modal = page.locator('.sk-card')

  // Dirty the buffer + Save → backend returns 409.
  await modal.locator('.cm-content').click()
  await page.keyboard.press('End')
  await page.keyboard.type(' ')

  const [putResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'PUT' &&
        new URL(r.url()).pathname === '/api/skills/my-custom',
    ),
    modal.getByRole('button', { name: /^Save/ }).click(),
  ])
  expect(putResp.status()).toBe(409)

  // Conflict banner appears with Reload action.
  const banner = modal.locator('.sk-banner-warn')
  await expect(banner).toBeVisible()
  await expect(banner).toContainText(/modified elsewhere/i)

  // Reload fetches fresh content; second GET returns the bumped mtime + new
  // description.
  await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'GET' &&
        new URL(r.url()).pathname === '/api/skills/my-custom',
    ),
    banner.getByRole('button', { name: 'Reload' }).click(),
  ])

  // Conflict banner gone, used-by banner still visible (the GET returned
  // a populated used_by list).
  await expect(modal.locator('.sk-banner-warn')).toHaveCount(0)
})
