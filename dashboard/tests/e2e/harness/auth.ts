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

/**
 * Seed a CSRF cookie before the app loads. Mirrors what
 * ``POST /api/auth/login`` would set in production. Use this for tests
 * that need to exercise authenticated-state behavior (probe whoami =
 * authenticated, mutations that carry X-CSRF-Token, …) without going
 * through the actual login flow.
 *
 * The session cookie itself is httpOnly and signed with the backend's
 * JWT_SECRET, which the harness can't realistically forge. Tests that
 * need a valid session use a fixture-mocked /api/auth/whoami that
 * always returns ``authenticated:true`` instead of forging the cookie.
 */
export async function seedCsrfCookie(
  page: Page,
  token: string = 'test-csrf-token-e2e'
): Promise<string> {
  await page.context().addCookies([
    {
      name: 'jarvis_csrf',
      value: token,
      // Cookies need a URL or domain+path. Use the same origin Playwright
      // navigates to so the browser will send it on /api/* requests.
      url: process.env.PW_BASE_URL || 'http://localhost:3000',
      sameSite: 'Lax',
    },
  ])
  return token
}

/**
 * Wipe everything the dashboard stores about auth: localStorage key,
 * session cookie, csrf cookie. Use in beforeEach for tests that simulate
 * a fresh-browser scenario.
 */
export async function clearAllAuth(page: Page): Promise<void> {
  await clearApiKey(page)
  const ctx = page.context()
  // Drop cookies for the test origin; clearCookies() with no args clears all.
  await ctx.clearCookies()
}
