/**
 * Seed the X-API-Key into localStorage before the app loads.
 *
 * The dashboard reads `localStorage.jarvis_api_key` synchronously in api.js,
 * so we must inject it via addInitScript (runs before any page script).
 */

import type { Page } from '@playwright/test'

const DEFAULT_TEST_KEY = 'test-api-key-e2e'

export async function seedApiKey(
  page: Page,
  key: string = DEFAULT_TEST_KEY
): Promise<void> {
  await page.addInitScript((k: string) => {
    try {
      window.localStorage.setItem('jarvis_api_key', k)
    } catch {
      // Some contexts (first navigation) may not have localStorage ready yet;
      // swallowing is safe — the next navigation re-runs the init script.
    }
  }, key)
}

/**
 * Clear the key — used to test the unauthenticated state (login redirect,
 * 401 handling, setup wizard first-run).
 */
export async function clearApiKey(page: Page): Promise<void> {
  await page.addInitScript(() => {
    try {
      window.localStorage.removeItem('jarvis_api_key')
    } catch {}
  })
}
