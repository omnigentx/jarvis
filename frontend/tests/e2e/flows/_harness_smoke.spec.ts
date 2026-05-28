/**
 * Smoke test for the harness itself — proves mockBackend intercepts, fixture
 * validation works, and NetworkRecorder captures calls. Not a real flow.
 */

import { expect, test, type Page, type Route } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')

// Serve a blank page at a synthetic path so the test origin matches the
// dashboard origin (relative fetches resolve) without actually loading the
// SPA (which would fire boot-time requests and clutter the harness
// assertions). The route handler runs before Vite preview, so it wins.
const BLANK_PATH = '/__harness_smoke_blank__'

async function gotoBlank(page: Page): Promise<void> {
  await page.route(`**${BLANK_PATH}`, (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: 'text/html',
      body: '<!doctype html><html><body></body></html>',
    }),
  )
  await page.goto(BLANK_PATH)
}

test('harness intercepts /api/* and records calls', async ({ page }) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, join(FIXTURES, '_harness_smoke.yaml'))
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await gotoBlank(page)
  const resp = await page.evaluate(async () => {
    const r = await fetch('/api/system/health', {
      headers: { 'X-API-Key': 'test-api-key-e2e' },
    })
    return { status: r.status, body: await r.json() }
  })

  expect(resp.status).toBe(200)
  expect(resp.body).toEqual({ status: 'ok' })

  recorder.assertContains('GET', '/api/system/health')
  expect(backend.unexpected.length).toBe(0)
})

test('harness fails loud on unmocked /api/* request', async ({ page }) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, join(FIXTURES, '_harness_smoke.yaml'))

  await gotoBlank(page)
  const resp = await page.evaluate(async () => {
    const r = await fetch('/api/not/in/fixture', {
      headers: { 'X-API-Key': 'test-api-key-e2e' },
    })
    return { status: r.status, body: await r.json() }
  })

  expect(resp.status).toBe(599)
  expect(resp.body.error).toBe('mock_backend_miss')
  expect(backend.unexpected.length).toBe(1)
  expect(backend.unexpected[0].path).toBe('/api/not/in/fixture')
})
