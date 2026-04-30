/**
 * Settings system flows — covers two user-visible features that shipped
 * via the `feature/ui-enhancement` PR but lacked end-to-end coverage:
 *
 *   1. Timezone field (Settings → General). User changes the IANA
 *      timezone, saves, and the request hits PUT /api/settings/system/
 *      TIMEZONE with the right body. The "Requires Restart" pill must
 *      be visible because the backend can't hot-reload the time-service
 *      MCP subprocess (stdio transport doesn't reconnect — see
 *      `fix(timezone): drop stdio-incompatible MCP restart at runtime`).
 *
 *   2. API enablement checklist (Settings → Services). After OAuth
 *      consent succeeds, Google still requires a per-API "Enable" click
 *      on Cloud Console — surfacing the deep-links is the only thing
 *      that prevents users from hitting 403 accessNotConfigured at
 *      runtime. The panel must render only when client_type != "none"
 *      AND required_apis.length > 0; we cover the happy path here, and
 *      the "empty list" negative control lives in settings-credentials
 *      (which uses required_apis: []).
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('Timezone — initial value renders, change saves via PUT and pill warns about restart', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'settings_general_timezone.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/settings')

  // Default tab = General. Scope every assertion to the row whose label
  // text matches "Timezone" — there are 3 rows in the System section
  // (Log Level / Timezone / Session) and "Save" buttons in each.
  const tzRow = page
    .locator('.setting-row')
    .filter({ has: page.getByText('Timezone', { exact: true }) })

  // Wait for the initial fetch to populate. The placeholder doubles as a
  // stable hook because the input has no aria-label.
  const tzInput = tzRow.locator('input[placeholder="Asia/Ho_Chi_Minh"]')
  await expect(tzInput).toHaveValue('Asia/Ho_Chi_Minh')

  // Pill: must say "Requires Restart" (NOT "Hot Reload" — easy regression
  // if someone copy-pastes the log-level row's pill class).
  await expect(tzRow.getByText('Requires Restart', { exact: true })).toBeVisible()

  // Save button is disabled until the input diverges from the saved value.
  const saveBtn = tzRow.getByRole('button', { name: 'Save', exact: true })
  await expect(saveBtn).toBeDisabled()

  // Type a new value and verify Save unlocks.
  await tzInput.fill('America/New_York')
  await expect(saveBtn).toBeEnabled()

  // Click + wait for PUT. We assert the URL pattern AND the body —
  // store.setValue sends `{ value, is_secret }` and the absence of either
  // would be a contract regression.
  const [putResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'PUT' &&
        new URL(r.url()).pathname === '/api/settings/system/TIMEZONE',
    ),
    saveBtn.click(),
  ])
  expect(putResp.status()).toBe(200)

  const putCall = recorder.assertContains('PUT', '/api/settings/system/TIMEZONE')
  const body = putCall.body as { value?: string; is_secret?: boolean }
  expect(body.value).toBe('America/New_York')
  expect(body.is_secret).toBe(false)

  // Success message renders (shared with the other system rows but the
  // PUT we just asserted is the only one that fired).
  await expect(page.getByText('Saved.', { exact: true })).toBeVisible()

  // After refreshEntry the input now reflects the persisted value, and
  // the Save button locks again because input == initialTimezone.
  await expect(tzInput).toHaveValue('America/New_York')
  await expect(saveBtn).toBeDisabled()

  expect(backend.unexpected.length).toBe(0)
})

test('API enablement checklist — renders deep-links for Gmail/Calendar after OAuth', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'settings_google_required_apis.yaml'),
  ])

  await page.goto('/settings')
  await page.getByRole('button', { name: 'Services', exact: true }).click()

  // Wait for the connected branch to resolve — "Connected" badge is the
  // stable signal that googleStatus arrived and `connected: true` was
  // applied.
  await expect(page.getByText('Connected', { exact: true })).toBeVisible()

  // Header of the enable panel — exact text comes from SettingsServices.vue
  // and is the user-facing call-to-action; if it changes, this test fails
  // loudly with the right diff in the report.
  await expect(
    page.getByText('Enable these APIs in Google Cloud Console', { exact: true }),
  ).toBeVisible()

  // Both APIs from the fixture should render as links pointing to the
  // backend-supplied enable_url. Use exact link names so a future change
  // to the api_id span doesn't accidentally match a different element.
  const gmailLink = page.getByRole('link', { name: /Gmail API/ })
  await expect(gmailLink).toBeVisible()
  await expect(gmailLink).toHaveAttribute(
    'href',
    'https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=987654321000',
  )
  // External-link hygiene — opening Cloud Console in the same tab would
  // wipe the user's setup progress.
  await expect(gmailLink).toHaveAttribute('target', '_blank')
  await expect(gmailLink).toHaveAttribute('rel', /noopener/)

  const calendarLink = page.getByRole('link', { name: /Google Calendar API/ })
  await expect(calendarLink).toBeVisible()
  await expect(calendarLink).toHaveAttribute(
    'href',
    'https://console.cloud.google.com/apis/library/calendar-json.googleapis.com?project=987654321000',
  )

  // The "couldn't parse a project number" fallback hint must NOT appear
  // when project_number is set in the response — that hint only kicks in
  // for malformed client_ids (covered indirectly by the unit tests in
  // backend/tests/test_routes/test_oauth.py).
  await expect(
    page.getByText("Couldn't parse a project number", { exact: false }),
  ).toHaveCount(0)

  expect(backend.unexpected.length).toBe(0)
})
