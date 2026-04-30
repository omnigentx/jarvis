/**
 * Wizard Step 3 — External Services, Google OAuth full inline flow.
 *
 * The wizard embeds <GoogleOAuthCard /> (shared with Settings → Services)
 * so the user can configure client credentials AND complete consent
 * without leaving the wizard. /settings is gated until setup completes,
 * so any "Open Settings" punt would just bounce the user back here.
 *
 * Coverage:
 *   1. UNCONFIGURED — client_type "none". Credential paste form renders
 *      INLINE (no "Open Settings" button). Saving creds calls
 *      PUT /api/oauth/google/client and re-fetches status; the form
 *      gives way to the next stage (e.g. Connect button for web client).
 *   2. CONFIGURED-NOT-CONNECTED (web) — Connect Google opens the OAuth
 *      popup; we stub window.open and assert the consent URL request.
 *   3. CONNECTED — already linked → "Connected" badge, no form, no
 *      Connect button.
 *   4. Regression guard — the old "OAuth — coming soon" wording must
 *      never reappear on Step 3.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

async function stubWindowOpen(page: import('@playwright/test').Page) {
  await page.addInitScript(() => {
    // @ts-ignore
    window.__openedUrls = []
    // @ts-ignore
    window.open = (url: string) => {
      // @ts-ignore
      window.__openedUrls.push(url)
      return {
        closed: false,
        close() { /* no-op */ },
      } as unknown as Window
    }
  })
}

test('UNCONFIGURED: credential paste form renders inline; saving creds advances state', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'wizard_services_google_unconfigured.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/setup/services')

  const card = page.getByTestId('google-oauth-card')
  await expect(card).toBeVisible()

  // Critical regression guard: the old "OAuth — coming soon" wording is
  // gone forever.
  await expect(page.getByText('OAuth — coming soon', { exact: false })).toHaveCount(0)
  // And the lazy "Open Settings" punt — wizards must let the user finish
  // setup without bouncing to the gated Settings page.
  await expect(page.getByText('Open Settings', { exact: false })).toHaveCount(0)

  // Credential form is visible INLINE.
  await expect(card.getByTestId('google-credentials-form')).toBeVisible()
  const idInput = card.getByTestId('google-client-id')
  const secretInput = card.getByTestId('google-client-secret')
  const saveBtn = card.getByTestId('google-save-credentials')
  await expect(idInput).toBeVisible()
  await expect(secretInput).toBeVisible()
  await expect(saveBtn).toBeVisible()

  // Type credentials and save — backend returns the new status (web,
  // not connected) so the form gives way to the Connect button.
  await idInput.fill('123456789012-test.apps.googleusercontent.com')
  await secretInput.fill('GOCSPX-test-secret')

  const [putResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'PUT' &&
        new URL(r.url()).pathname === '/api/oauth/google/client',
    ),
    saveBtn.click(),
  ])
  expect(putResp.status()).toBe(200)

  // Body contract: client_id / client_secret / client_type all sent.
  const putCall = recorder.assertContains('PUT', '/api/oauth/google/client')
  const body = putCall.body as {
    client_id?: string
    client_secret?: string
    client_type?: string
  }
  expect(body.client_id).toBe('123456789012-test.apps.googleusercontent.com')
  expect(body.client_secret).toBe('GOCSPX-test-secret')
  expect(['desktop', 'web']).toContain(body.client_type)

  // After re-fetch, the fixture's SECOND /status response is "web,
  // not connected" — Connect button now visible, form gone.
  await expect(card.getByTestId('google-connect-btn')).toBeVisible()
  await expect(card.getByTestId('google-credentials-form')).toHaveCount(0)

  expect(backend.unexpected.length).toBe(0)
})

test('CONFIGURED-NOT-CONNECTED: Connect Google fires POST /oauth/google/start and opens consent popup', async ({
  page,
}) => {
  await seedApiKey(page)
  await stubWindowOpen(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'wizard_services_google_ready.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/setup/services')

  const card = page.getByTestId('google-oauth-card')
  await expect(card.getByTestId('google-badge-not-connected')).toBeVisible()

  const connectBtn = card.getByTestId('google-connect-btn')
  await expect(connectBtn).toBeEnabled()

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
  const body = startCall.body as { redirect_uri?: string }
  expect(body.redirect_uri).toMatch(/\/oauth\/callback$/)

  // Button label flips to "Waiting for consent…" because the stubbed
  // window.open returns a non-null popup.
  await expect(card.getByTestId('google-connect-btn')).toHaveText(
    /waiting for consent/i,
  )

  const opened = await page.evaluate(
    // @ts-ignore
    () => (window as any).__openedUrls as string[],
  )
  expect(opened).toHaveLength(1)
  expect(opened[0]).toContain('accounts.google.com')

  expect(backend.unexpected.length).toBe(0)
})

test('CONNECTED: already linked → "Connected" badge, no form, no Connect button', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'wizard_services_google_connected.yaml'),
  ])

  await page.goto('/setup/services')

  const card = page.getByTestId('google-oauth-card')
  await expect(card.getByTestId('google-badge-connected')).toBeVisible()
  await expect(card.getByTestId('google-badge-connected')).toHaveText('Connected')

  await expect(card.getByTestId('google-connect-btn')).toHaveCount(0)
  await expect(card.getByTestId('google-credentials-form')).toHaveCount(0)
  await expect(page.getByText('OAuth — coming soon', { exact: false })).toHaveCount(0)

  expect(backend.unexpected.length).toBe(0)
})
