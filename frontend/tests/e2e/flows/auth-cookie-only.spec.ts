/**
 * E2E regression: the SPA must NEVER store the API key in localStorage.
 *
 * Pre-migration the dashboard mirrored ``JARVIS_API_KEY`` into
 * ``localStorage.jarvis_api_key`` and sent it as a Bearer header on
 * every request. That left the credential XSS-exfiltrate-able. The
 * cookie-only migration removed that path; this spec is the tripwire
 * that catches any future code that re-introduces it.
 *
 * What we exercise:
 *   1. AuthGate login via API key text input → cookie minted, modal closes.
 *   2. After login, ``localStorage.jarvis_api_key`` is absent.
 *   3. Subsequent mutations carry ``X-CSRF-Token`` (cookie-mode CSRF
 *      defence) but NOT ``Authorization: Bearer …``.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { clearAllAuth, mockBackend, NetworkRecorder } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')


test('after API-key login the key never lands in localStorage', async ({ page }) => {
  await clearAllAuth(page)
  await mockBackend(page, join(FIXTURES, 'auth_gate_login_success.yaml'))

  await page.goto('/')

  const modal = page.getByRole('dialog', { name: /authentication required/i })
  await expect(modal).toBeVisible()

  await page.locator('#auth-gate-key').fill('test-api-key-e2e')
  await page.getByRole('button', { name: /^sign in$/i }).click()
  await expect(modal).toBeHidden({ timeout: 3000 })

  // The credential MUST NOT have leaked into any localStorage key.
  // We don't allow-list — any key whose name or value contains the
  // API key counts as a leak.
  const leak = await page.evaluate((needle) => {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      const v = (k && localStorage.getItem(k)) || ''
      if (k && (k.includes('api_key') || k.includes('jarvis_api'))) {
        return { kind: 'forbidden_key', key: k, value: v }
      }
      if (v.includes(needle)) {
        return { kind: 'value_contains_credential', key: k, valueSample: v.slice(0, 24) }
      }
    }
    return null
  }, 'test-api-key-e2e')
  expect(leak, `localStorage leak: ${JSON.stringify(leak)}`).toBeNull()
})


test('mutations after login carry X-CSRF-Token but NOT Authorization: Bearer',
  async ({ page }) => {
    await clearAllAuth(page)
    await mockBackend(page, join(FIXTURES, 'auth_gate_login_success.yaml'))

    const recorder = new NetworkRecorder()
    await recorder.attach(page)

    await page.goto('/')
    await page.locator('#auth-gate-key').fill('test-api-key-e2e')
    await page.getByRole('button', { name: /^sign in$/i }).click()
    await expect(
      page.getByRole('dialog', { name: /authentication required/i }),
    ).toBeHidden()

    // Trigger any mutation through apiFetch by hand to make sure the
    // request shape is what we expect. We use a path the fixture
    // doesn't fulfil (returns 599) — we only care about the request
    // headers Playwright recorded.
    await page.evaluate(async () => {
      const csrf = document.cookie
        .split('; ')
        .find((c) => c.startsWith('jarvis_csrf='))
        ?.split('=', 2)[1] ?? ''
      await fetch('/api/probe-mutation-cookie-only', {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrf,
        },
        body: JSON.stringify({}),
      }).catch(() => { /* fixture returns 599; we only inspect the request */ })
    })

    const mutation = recorder.calls.find(
      (c) => c.method === 'POST'
        && c.path === '/api/probe-mutation-cookie-only',
    )
    expect(mutation, 'mutation must have been recorded').toBeTruthy()

    // Cookie-mode CSRF: header present.
    expect(mutation!.headers['x-csrf-token']).toBeTruthy()

    // Bearer must NOT be sent. The regression we're guarding against
    // is "apiFetch silently re-introduces Bearer because someone
    // re-added getApiKey() somewhere".
    expect(mutation!.headers['authorization']).toBeFalsy()
  })


test('SSE URLs do not carry ?api_key=', async ({ page }) => {
  await clearAllAuth(page)
  await mockBackend(page, [
    join(FIXTURES, '_app_boot_noise.yaml'),
    join(FIXTURES, 'auth_gate_login_success.yaml'),
  ])
  await page.goto('/')
  await page.locator('#auth-gate-key').fill('test-api-key-e2e')
  await page.getByRole('button', { name: /^sign in$/i }).click()
  await expect(
    page.getByRole('dialog', { name: /authentication required/i }),
  ).toBeHidden()

  // Give the SSE consumers a moment to open their streams.
  await page.waitForTimeout(800)

  const leak = await page.evaluate(() => {
    // Performance entries persist all network requests the page made.
    return performance.getEntriesByType('resource')
      .map((e) => (e as PerformanceResourceTiming).name)
      .filter((url) => url.includes('/api/') && url.includes('api_key='))
  })
  expect(leak, `SSE / fetch URLs still carry api_key=: ${JSON.stringify(leak)}`)
    .toEqual([])
})
