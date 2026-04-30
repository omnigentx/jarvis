/**
 * E2E flow: Settings → Services (Google OAuth + credential forms).
 *
 * Guards the post-setup credential management surface:
 *   1. Fresh "web-app" OAuth client on file, no tokens yet → clicking
 *      "Connect Google" POSTs /api/oauth/google/start and the UI flips
 *      to a "Waiting for consent…" state. We stub window.open so the
 *      consent popup never escapes to real Google.
 *   2. Negative control: tokens already on file → UI shows "Connected"
 *      + Disconnect; confirming the dialog fires DELETE /api/oauth/google.
 *   3. Save-credential error: PUT /api/oauth/google/client returns 400,
 *      the UI must render the backend's detail message — no silent
 *      success.
 *
 * OAuth popup caveat: a real consent screen cannot complete in-test
 * (it hits Google). We assert the backend contract was honoured and
 * stop there — we do NOT simulate the callback.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

/**
 * Install a fake window.open BEFORE the page loads. Real window.open would
 * navigate to https://accounts.google.com and break the test; the stub
 * records the URL it was called with and returns a handle with the
 * `.closed` flag the SettingsServices popupWatcher polls.
 */
async function stubWindowOpen(page: import('@playwright/test').Page) {
  await page.addInitScript(() => {
    // @ts-ignore — attach for test introspection
    window.__openedUrls = []
    // @ts-ignore
    window.open = (url: string) => {
      // @ts-ignore
      window.__openedUrls.push(url)
      return {
        closed: false,
        close() {
          /* no-op — SettingsServices calls popup.close() on success */
        },
      } as unknown as Window
    }
  })
}

test('not-connected: clicking Connect Google fires POST /oauth/google/start and UI flips to waiting state', async ({
  page,
}) => {
  await seedApiKey(page)
  await stubWindowOpen(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'settings_google_not_connected.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/settings')

  // Switch to Services tab — General is the default mount.
  await page.getByRole('button', { name: 'Services', exact: true }).click()

  // Wait for the Google card to resolve loading. The "Not Connected" badge
  // is the stable signal that googleStatus arrived and branch C is live.
  await expect(page.getByText('Not Connected')).toBeVisible()

  const connectBtn = page.getByRole('button', { name: /connect google/i })
  await expect(connectBtn).toBeVisible()

  // Click + wait for the POST to complete. NetworkRecorder captures the
  // body so we can assert the redirect_uri contract.
  const [startResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname === '/api/oauth/google/start',
    ),
    connectBtn.click(),
  ])
  expect(startResp.status()).toBe(200)

  const startCall = recorder.assertContains('POST', '/api/oauth/google/start')
  const body = startCall.body as { redirect_uri?: string } | null
  // Frontend builds redirect_uri from window.location.origin + /oauth/callback.
  expect(body?.redirect_uri).toMatch(/\/oauth\/callback$/)

  // UI transition: button label flips to "Waiting for consent…" because
  // window.open returned our non-null stub (popup != null keeps
  // `connecting` true).
  await expect(
    page.getByRole('button', { name: /waiting for consent/i }),
  ).toBeVisible()

  // And window.open was invoked with the consent URL from the fixture.
  const openedUrls = await page.evaluate(
    // @ts-ignore
    () => (window as any).__openedUrls as string[],
  )
  expect(openedUrls).toHaveLength(1)
  expect(openedUrls[0]).toContain('accounts.google.com')

  expect(backend.unexpected.length).toBe(0)
})

test('connected (negative control): Disconnect fires DELETE /api/oauth/google', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'settings_google_connected.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/settings')
  await page.getByRole('button', { name: 'Services', exact: true }).click()

  // Connected state rendered (branch D).
  await expect(page.getByText('Connected', { exact: true })).toBeVisible()
  await expect(page.getByText(/2 granted/)).toBeVisible()

  // Click Disconnect (the card's ghost-danger button) → confirm modal opens.
  // Before the modal is teleported to <body> there is only ONE Disconnect
  // button on the page.
  await page.getByRole('button', { name: 'Disconnect', exact: true }).click()

  // Modal lives in .cm-overlay (Teleport target). Scope to it so the
  // card's Disconnect button doesn't collide with the confirm button.
  const modal = page.locator('.cm-overlay')
  await expect(modal).toBeVisible()
  const confirmBtn = modal.getByRole('button', { name: 'Disconnect', exact: true })
  await expect(confirmBtn).toBeVisible()

  const [delResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'DELETE' &&
        new URL(r.url()).pathname === '/api/oauth/google',
    ),
    confirmBtn.click(),
  ])
  expect(delResp.status()).toBe(200)

  recorder.assertContains('DELETE', '/api/oauth/google')

  // After DELETE the component re-fetches /status; the fixture's second
  // response is `connected: false`, so the UI must flip to the
  // not-connected branch — "Connect Google" button appears, the
  // "Connected" badge disappears. Asserting this catches a regression
  // where disconnect fires the DELETE but forgets to trigger the re-fetch.
  await expect(page.getByRole('button', { name: /Connect Google/i })).toBeVisible()
  await expect(page.getByText('Connected', { exact: true })).toHaveCount(0)

  expect(backend.unexpected.length).toBe(0)
})

test('save-credential error: backend 400 surfaces as visible error, not silent success', async ({
  page,
}) => {
  await seedApiKey(page)
  await stubWindowOpen(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'settings_google_not_connected.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/settings')
  await page.getByRole('button', { name: 'Services', exact: true }).click()

  // Branch C (web, not connected) shows a "Change credentials" button that
  // reveals the credentials form. Scope to the Google card so locators
  // don't collide with the Roborock / GitHub cards (which both also
  // render a "Save credentials" button when their state is empty).
  const googleCard = page.locator('section.panel-card').filter({
    has: page.getByRole('heading', { name: /google \(gmail \+ calendar\)/i }),
  })
  await expect(googleCard.getByText('Not Connected')).toBeVisible()
  await googleCard.getByRole('button', { name: /change credentials/i }).click()

  // Form inputs by placeholder (no <label> elements on these inputs).
  await googleCard.getByPlaceholder('Client ID').fill('bad-client-id')
  await googleCard.getByPlaceholder('Client Secret').fill('bad-secret')

  const [putResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'PUT' &&
        new URL(r.url()).pathname === '/api/oauth/google/client',
    ),
    googleCard.getByRole('button', { name: 'Save credentials', exact: true }).click(),
  ])
  expect(putResp.status()).toBe(400)

  const putCall = recorder.assertContains('PUT', '/api/oauth/google/client')
  const body = putCall.body as
    | { client_id?: string; client_secret?: string; client_type?: string }
    | null
  expect(body?.client_id).toBe('bad-client-id')
  expect(body?.client_secret).toBe('bad-secret')

  // Error from fixture detail is surfaced to the user — no silent pass-through.
  await expect(
    page.getByText(/Invalid client_id/i),
  ).toBeVisible()

  // And critically: no "Credentials saved" success banner appeared.
  await expect(page.getByText(/Credentials saved\./i)).toHaveCount(0)

  expect(backend.unexpected.length).toBe(0)
})
